//! M4 — page-evict vs pure-GPU disambiguator for the multi-second
//! transition paint stall M3 cornered.
//!
//! ## What M3 found
//!
//! M3 (commit `1d62072`) drove the REAL `paint_and_present_one_
//! transition_frame` end-to-end through warm-park-resume + the
//! offscreen FBO bake (`is_offscreen_bake=true`) + the FS_FADE
//! cross-fade + the Path-B consumer poll. QA's sub-agent verified
//! the journal for 10 sweep runs (5 parks × FLUSH=on/off):
//!
//! - All 3 pixel-failure modes CLEAN. `transition_tex_probe side=b`
//!   never luma=0; `paint_transition_reuse_cached_b` count = 0;
//!   `transition_screen_oracle tag=PAINTED luma=N all_constant=false`
//!   every transition tick — composite is always correct.
//! - But `paint_stall_us` was MULTI-SECOND on 2/10 runs:
//!   `park1000-flush=on=12.0s` and `park10000-flush=off=6.248s`.
//!
//! Verdict: the freeze is a multi-second PAINT STALL — the correct
//! frame frozen in time, NOT black/stale. Root cause:
//!   - TRIGGER: the offscreen-bake `gl.flush()` at hdmi.rs:9160
//!     serializing against the V3D backlog
//!   - AMPLIFIER: memory page-eviction matching the perf-arc's
//!     documented page-evict freeze profile
//!
//! `OPENMARQUEE_BAKE_OFFSCREEN_FLUSH=on/off` only moves WHICH
//! instruction blocks; the stall persists either way. MSDF-storm
//! REFUTED (init-time labels only).
//!
//! Fix direction = FOOTPRINT REDUCTION (the documented perf-arc),
//! NOT a transition rewrite. But to confirm we don't fix the wrong
//! thing, we need to disambiguate page-evict vs pure-GPU drain.
//!
//! ## What M4 adds vs M3
//!
//! M4 is M3 + 3 instrumentation lanes around each transition tick:
//!
//! 1. `getrusage(RUSAGE_SELF).ru_majflt` delta — sampled BEFORE
//!    + AFTER `paint_and_present_one_transition_frame`. HIGH delta
//!    during the stall = the kernel is reading anonymous-mapped
//!    pages back from swap or page-cache-evicted file pages =
//!    PAGE-EVICT root cause confirmed.
//! 2. VmRSS + VmSwap from `/proc/self/status` — sampled at
//!    transition entry + transition exit. Confirms the working-set
//!    size + swap pressure at the moment of the freeze.
//! 3. Existing tail_diag markers (`tail_diag_blit_flush` >500ms,
//!    `tail_diag_bake_breakdown` >100ms) auto-fire on the stall.
//!
//! ## Verdict per stalled tick
//!
//! - HIGH majflt (e.g. >50/tick) during the stall → page-evict
//!   is the dominant root cause → footprint reduction (the perf-arc)
//!   is THE fix and sufficient.
//! - ~0 majflt + multi-second `flush_us` → pure GPU drain →
//!   we ALSO need a GPU/barrier fix (glFenceSync vs full
//!   `gl.flush()`, decouple-feed-from-paint, etc).
//! - Mixed → both arcs needed; footprint first then re-measure.
//!
//! M3 leans ~70/30 page-evict per the perf-arc history.
//!
//! ## Run matrix (per QA dispatch)
//!
//! Re-run ONLY the 2 catastrophic configs ~4-5x each — the spike
//! is ~2/10 = probabilistic so single runs aren't reliable:
//!
//!   sudo systemctl stop openmarquee-backend
//!   for run in 1 2 3 4 5; do
//!     M0_PARK_MS=1000 OPENMARQUEE_BAKE_OFFSCREEN_FLUSH=on \
//!       M1_VIDEO_A=... M1_VIDEO_B=... \
//!       /usr/local/bin/m4-pageevict-probe 2>&1 | tee \
//!         /tmp/m4_park1000_flushon_run${run}.log
//!     M0_PARK_MS=10000 OPENMARQUEE_BAKE_OFFSCREEN_FLUSH=off \
//!       M1_VIDEO_A=... M1_VIDEO_B=... \
//!       /usr/local/bin/m4-pageevict-probe 2>&1 | tee \
//!         /tmp/m4_park10000_flushoff_run${run}.log
//!   done
//!   sudo systemctl start openmarquee-backend
//!
//! ## Where M4 lives in the M0/M1/M2/M3/M4 chain
//!
//! - M0: single-decoder park-resume → CLEAN.
//! - M1: two-decoder contention → CLEAN.
//! - M2 v3 (β surface): warm-park-resume + real bake + real swap on
//!   the WINDOW FBO (`is_offscreen_bake=false`, eglSwapBuffers IS the
//!   tile-store barrier) → HEALTHY. Cleared the steady-state paint
//!   path. But did NOT exercise:
//!     1. The offscreen-FBO bake (`is_offscreen_bake=true`) where the
//!        r76 "FBO stores BLACK" race lives (iter-7 root-cause comment
//!        at hdmi.rs:8584).
//!     2. The cross-fade compositor (FS_FADE shader blends `u_src_a` +
//!        `u_src_b` with `u_t`).
//!     3. The Path-B consumer poll deadline
//!        (OPENMARQUEE_BAKE_B_POLL_DEADLINE_MS default 100ms; 4-iter
//!        cap or 60 under the head-start gate) and its stale-buffer-
//!        reuse fallback (`paint_transition_reuse_cached_b`).
//!
//! r76 lives in EXACTLY that untested code. M3 corners it.
//!
//! ## The strongest lead M3 must measure
//!
//! M2 v3's first-frame-after-resume paint stall GREW with park
//! duration: 95ms@50, 165ms@1s, 484ms@10s. If B's first post-resume
//! frame in the REAL transition takes >100ms, the Path-B consumer poll
//! gives up + the transition reuses the cached/stale FBO + buf_idx →
//! "side-B locks to the prior slide's stale buffer" = the exact freeze
//! forensic captured in a64cbbb / qa/sideb-buffer-trace branch.
//!
//! ## What M3 drives
//!
//! Per-tick inside `hdmi::run_in_egl_session`:
//!
//!   PreOpen+BParked (A solo): `hdmi::bake_video_slide_to_current_fbo(
//!     session, &dem_a.samples, ..., false /* window FB */)` + real
//!     `finish_video_slide_swap_and_commit` — keeps A on the glass +
//!     advances A's decoder, exactly the M2 v3 steady-state path.
//!
//!   Transition (progress 0.0 → 1.0 over `M3_TRANSITION_TICKS`):
//!     `hdmi::paint_and_present_one_transition_frame(session, &card,
//!       TransitionEndpoint::Video{...A}, TransitionEndpoint::Video{...B},
//!       None, None, "fade", progress)` — runs:
//!       (a) offscreen FBO bake of A into tex_a (is_offscreen_bake=true)
//!       (b) offscreen FBO bake of B into tex_b (is_offscreen_bake=true)
//!           through the Path-B consumer poll path
//!       (c) FS_FADE blend pass mixing tex_a + tex_b by progress
//!       (d) commit_fb (set_crtc / page_flip)
//!     The transition fn's built-in `[perf] transition_tex_probe
//!     side=a/b ... luma=N` auto-fires once at progress ≥ 0.4 — that's
//!     M3's primary FBO oracle. Path-B's `[perf] bake_b_poll_outcome
//!     ... reason=...` + `[perf] paint_transition_reuse_cached_b ...`
//!     also emit to stderr (the journal in QA's run).
//!
//!   M3 ALSO does a post-transition `glReadPixels` on the window back
//!   buffer at progress=1.0 (B fully visible) → window screen oracle
//!   on B's final composited contribution.
//!
//! ## Failure signatures M3 captures (per QA dispatch)
//!
//! 1. **r76 PROPER (offscreen FBO black):** `transition_tex_probe side=b
//!    luma=0` (or all_constant on the M3 window readback). To confirm
//!    the iter-7 flush is load-bearing: re-run with
//!    `OPENMARQUEE_BAKE_OFFSCREEN_FLUSH=off` → the black should
//!    REAPPEAR if flush was the real fix.
//!
//! 2. **STALE-BUFFER LOCK (the timing lead):**
//!    `paint_transition_reuse_cached_b` line in stderr with a frozen
//!    `last_sideb_buf_idx` AND M3's window oracle stuck across ticks.
//!    Cross-reference with `bake_b_poll_outcome reason=deadline_
//!    exhausted` / `decouple_skip_pathb` + the M3 summary's
//!    `b_first_frame_us` field vs the ~100ms deadline.
//!
//! 3. **BLEND-PASS BLACK:** `transition_tex_probe` shows BOTH side=a
//!    luma>0 AND side=b luma>0, but the M3 final window readback is
//!    black/stuck → localizes the bug to FS_FADE / scene-fb / present.
//!
//! ## Build-time visibility (no new pub flips needed)
//!
//! M2 Phase 2 (β surface, commit 6412a26) already shipped the pub
//! flips M3 needs: `bake_video_slide_to_current_fbo` /
//! `finish_video_slide_swap_and_commit` / `EglSession::mode_w/mode_h/gl`.
//! `paint_and_present_one_transition_frame` + `TransitionEndpoint`
//! were already pub from a prior arc. M3 needs ZERO additional pub
//! surface in hdmi.rs.
//!
//! ## Final summary line (M0/M1/M2 grep-uniform)
//!
//! `[m4] PARK_MS=N B_OPEN_MS=N a_warm_us=N b_warm_us=N b_resume_us=N
//!  b_first_frame_us=N transitions=N a_pretransition=N
//!  b_transition_frames=N final_screen_pixel_ok=N/N
//!  final_screen_total=N transition_kind="fade"
//!  offscreen_flush=on|off
//!  bake_b_poll_deadline_ms=N b_open_errno="..." last_errno="..."
//!  paint_stall_us=N VERDICT=HEALTHY|DEGRADED|DIVERGENT|WEDGED`.
//!
//! Per QA dispatch: M3 emits its summary; QA also greps stderr for
//! `transition_tex_probe` + `bake_b_poll_outcome` +
//! `paint_transition_reuse_cached_b` — those auto-emit from prod's
//! own instrumentation, not from M3.
//!
//! ## Run recipe (fireplacesign, backend stopped, manual)
//!
//!   sudo systemctl stop openmarquee-backend
//!   for park_ms in 50 200 1000 5000 10000; do
//!     for flush in on off; do
//!       M0_PARK_MS=$park_ms \
//!       M1_VIDEO_A=/var/openmarquee/content/<uuidA>/asset.mp4 \
//!       M1_VIDEO_B=/var/openmarquee/content/<uuidB>/asset.mp4 \
//!       OPENMARQUEE_BAKE_OFFSCREEN_FLUSH=$flush \
//!       /usr/local/bin/m3-transition-probe 2>&1 | tee \
//!         /tmp/m3_park${park_ms}_flush${flush}.log
//!     done
//!   done
//!   sudo systemctl start openmarquee-backend  # restore
//!
//! Branch: `task/frame-phase-instrument-2026-06-16` (M2 Phase 2 at
//! 6412a26 is the base).

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("m4-pageevict-probe: Linux-only (V4L2 + DRM/KMS + EGL + getrusage).");
    std::process::exit(1);
}

#[cfg(target_os = "linux")]
fn main() -> anyhow::Result<()> {
    linux_main::run()
}

#[cfg(target_os = "linux")]
mod linux_main {
    use anyhow::{anyhow, Context, Result};
    use openmarquee_render::{hdmi, mp4_demux, probe_oracle, video_decode, Card};
    use std::path::Path;
    use std::time::{Duration, Instant};

    /// `getrusage(RUSAGE_SELF).ru_majflt` — major page faults
    /// (those that required disk I/O: swap-in or page-cache evict-
    /// then-fetch). The signal we want: HIGH delta across a single
    /// transition tick = the kernel had to read pages back from
    /// somewhere on disk during the stall → page-evict.
    ///
    /// Returns `None` on getrusage failure. Per sacred review
    /// 2026-06-17: a sentinel -1 would corrupt downstream delta
    /// arithmetic (a pre=large, post=-1 case → large-negative
    /// delta tanking sum_majflt_delta_transition + max_majflt_
    /// delta_tick could miss the page-evict signature → flip the
    /// verdict to PURE_GPU_DRAIN_PROBABLE — the EXACT inversion
    /// the probe exists to prevent). Callers MUST handle None
    /// (skip the stat update for that tick).
    fn read_majflt() -> Option<i64> {
        let mut usage: libc::rusage = unsafe { std::mem::zeroed() };
        let rc = unsafe { libc::getrusage(libc::RUSAGE_SELF, &mut usage as *mut _) };
        if rc != 0 {
            return None;
        }
        Some(usage.ru_majflt as i64)
    }

    /// `getrusage(RUSAGE_SELF).ru_minflt` — minor page faults
    /// (resolved without disk I/O). Useful side channel: high
    /// minflt + low majflt = lots of CoW or first-touch but no
    /// disk pressure; high majflt = the page-evict signature.
    ///
    /// Returns `None` on failure (same rationale as `read_majflt`).
    fn read_minflt() -> Option<i64> {
        let mut usage: libc::rusage = unsafe { std::mem::zeroed() };
        let rc = unsafe { libc::getrusage(libc::RUSAGE_SELF, &mut usage as *mut _) };
        if rc != 0 {
            return None;
        }
        Some(usage.ru_minflt as i64)
    }

    /// Parses `/proc/self/status` for `VmRSS:` + `VmSwap:` (KB).
    /// Returns `(rss_kb, swap_kb)` — both `0` on read/parse error
    /// (defensive: if /proc isn't mounted, M4 still runs).
    fn read_vmrss_vmswap_kb() -> (u64, u64) {
        let content = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
        let mut rss = 0u64;
        let mut swap = 0u64;
        for line in content.lines() {
            if let Some(rest) = line.strip_prefix("VmRSS:") {
                if let Some(n) = rest.split_whitespace().next() {
                    rss = n.parse().unwrap_or(0);
                }
            } else if let Some(rest) = line.strip_prefix("VmSwap:") {
                if let Some(n) = rest.split_whitespace().next() {
                    swap = n.parse().unwrap_or(0);
                }
            }
        }
        (rss, swap)
    }

    enum Phase {
        ASoloPreOpen,
        BOpening,
        BParked,
        Transition,
        Drain,
    }

    impl Phase {
        fn as_str(&self) -> &'static str {
            match self {
                Phase::ASoloPreOpen => "preopen",
                Phase::BOpening => "opening",
                Phase::BParked => "parked",
                Phase::Transition => "transition",
                Phase::Drain => "drain",
            }
        }
    }

    pub fn run() -> Result<()> {
        // 2026-06-17 — auto-enable the env-gated pre-swap screen
        // oracle in paint_and_present_one_transition_frame so the
        // M3 binary captures BLEND-PASS BLACK (signature #3) by
        // default. QA can still disable via
        // OPENMARQUEE_TRANSITION_SCREEN_ORACLE=off if needed for
        // an A/B run. SetEnv is process-local (M3 is the only thing
        // running this binary), so the IPC sidecar in another
        // process is unaffected.
        if std::env::var("OPENMARQUEE_TRANSITION_SCREEN_ORACLE").is_err() {
            std::env::set_var("OPENMARQUEE_TRANSITION_SCREEN_ORACLE", "on");
        }

        // ---------- env ------------------------------------------------
        let park_ms: u64 = std::env::var("M0_PARK_MS").ok()
            .and_then(|s| s.parse().ok()).unwrap_or(200);
        let b_open_ms: u64 = std::env::var("M1_B_OPEN_MS").ok()
            .and_then(|s| s.parse().ok()).unwrap_or(2000);
        let video_a_path = std::env::var("M1_VIDEO_A")
            .context("M1_VIDEO_A env var unset")?;
        let video_b_path = std::env::var("M1_VIDEO_B")
            .context("M1_VIDEO_B env var unset")?;
        let transition_kind: String = std::env::var("M3_TRANSITION_KIND")
            .unwrap_or_else(|_| "fade".to_string());
        let transition_ticks: u32 = std::env::var("M3_TRANSITION_TICKS").ok()
            .and_then(|s| s.parse().ok()).unwrap_or(30);
        // Surface the read of these env vars (which hdmi.rs reads
        // internally) into the summary so the sub-agent + QA can
        // cross-correlate without grepping the bin's environment.
        let offscreen_flush_env = std::env::var("OPENMARQUEE_BAKE_OFFSCREEN_FLUSH")
            .unwrap_or_else(|_| "(default-on)".to_string());
        let bake_b_poll_deadline_ms = std::env::var("OPENMARQUEE_BAKE_B_POLL_DEADLINE_MS")
            .unwrap_or_else(|_| "(default-100)".to_string());

        eprintln!(
            "[m4] params PARK_MS={park_ms} B_OPEN_MS={b_open_ms} KIND={transition_kind} \
             TICKS={transition_ticks} OFFSCREEN_FLUSH={offscreen_flush_env} \
             BAKE_B_POLL_DEADLINE_MS={bake_b_poll_deadline_ms} A={video_a_path} B={video_b_path}",
        );

        // ---------- DRM device ----------------------------------------
        let card = Card::open(Path::new("/dev/dri/card0"))
            .or_else(|_| Card::open(Path::new("/dev/dri/card1")))
            .context("open /dev/dri/card{0,1}")?;

        // ---------- Demux + prime A -----------------------------------
        let dem_a = mp4_demux::Mp4Demuxer::open(Path::new(&video_a_path))
            .with_context(|| format!("Mp4Demuxer::open A {video_a_path}"))?;
        let slide_a_id = uuid::Uuid::from_bytes([
            0x4d,0x33,0xaa,0xaa, 0,0,0,0, 0,0,0,0, 0,0,0,0,
        ]);
        let t_a_warm = Instant::now();
        let mut state_a = video_decode::prime_video_decoder_for_preload(&dem_a, slide_a_id)
            .map_err(|e| anyhow!("prime A: {e:#}"))?;
        let a_warm_us = t_a_warm.elapsed().as_micros();
        eprintln!(
            "[m4] A primed warm_us={} samples={} w={} h={}",
            a_warm_us, dem_a.samples.len(), dem_a.width, dem_a.height,
        );

        // 2026-06-17 M4 — baseline memory snapshot AFTER A primed.
        // Captures the post-prime working set for the page-evict
        // analysis (M3's per-arc footprint reduction hypothesis
        // predicts cma_used + VmRSS pressure climb during the
        // transition window).
        let (vmrss_post_a_prime, vmswap_post_a_prime) = read_vmrss_vmswap_kb();
        let majflt_post_a_prime = read_majflt().unwrap_or(-1);
        let minflt_post_a_prime = read_minflt().unwrap_or(-1);
        eprintln!(
            "[m4] mem_after_a_prime vmrss_kb={} vmswap_kb={} majflt={} minflt={}",
            vmrss_post_a_prime, vmswap_post_a_prime,
            majflt_post_a_prime, minflt_post_a_prime,
        );

        // ---------- Demux B (open deferred) ---------------------------
        let dem_b = mp4_demux::Mp4Demuxer::open(Path::new(&video_b_path))
            .with_context(|| format!("Mp4Demuxer::open B {video_b_path}"))?;
        let slide_b_id = uuid::Uuid::from_bytes([
            0x4d,0x33,0xbb,0xbb, 0,0,0,0, 0,0,0,0, 0,0,0,0,
        ]);
        eprintln!(
            "[m4] B mp4 parsed samples={} w={} h={}",
            dem_b.samples.len(), dem_b.width, dem_b.height,
        );

        // ---------- Drive inside the REAL EGL session ------------------
        hdmi::run_in_egl_session(&card, 0, |session| {
            tick_loop(
                session, &card,
                dem_a, &mut state_a, a_warm_us,
                dem_b, slide_b_id,
                park_ms, b_open_ms,
                &transition_kind, transition_ticks,
                offscreen_flush_env,
                bake_b_poll_deadline_ms,
            )
        })?;
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn tick_loop(
        session: &mut hdmi::EglSession<'_>,
        card: &Card,
        dem_a: mp4_demux::Mp4Demuxer,
        state_a: &mut video_decode::VideoDecoderState,
        a_warm_us: u128,
        dem_b: mp4_demux::Mp4Demuxer,
        slide_b_id: uuid::Uuid,
        park_ms: u64,
        b_open_ms: u64,
        transition_kind: &str,
        transition_ticks: u32,
        offscreen_flush_env: String,
        bake_b_poll_deadline_ms: String,
    ) -> Result<()> {
        let mode_w = session.mode_w() as u32;
        let mode_h = session.mode_h() as u32;
        eprintln!("[m4] session up mode={}x{}", mode_w, mode_h);

        let mut phase = Phase::ASoloPreOpen;
        let mut state_b: Option<video_decode::VideoDecoderState> = None;
        let mut park_until: Option<Instant> = None;
        let mut transition_start: Option<Instant> = None;
        let mut transition_tick_idx: u32 = 0;

        // Oracles + counters.
        //
        // M3's screen oracle for BLEND-PASS BLACK (failure signature
        // #3) is the env-gated `transition_screen_oracle` prod probe
        // that hdmi.rs:paint_and_present_one_transition_frame emits
        // PRE-SWAP (the readback before the swap moves the composite
        // to FRONT). M3 sets OPENMARQUEE_TRANSITION_SCREEN_ORACLE=on
        // at startup so the prod probe fires every transition tick.
        // The probe's stderr line carries luma + all_constant + tag
        // (PAINTED / SKIPPED) which QA greps from the journal.
        //
        // For M3's IN-PROCESS summary, we use the pub accessor
        // `hdmi::take_last_transition_paint_outcome()` to know
        // whether each tick was PAINTED (real composite presented),
        // SKIPPED (FYS-bug-C no-frame-ready early-return), or
        // unknown (accessor returned None, shouldn't happen since
        // we auto-enable the env).
        let mut a_solo_screen_oracle = probe_oracle::PixelOracle::new();

        let mut a_pretransition: u32 = 0;
        let mut b_transition_frames: u32 = 0;   // count of Ok(()) returns (legacy, may incl. skips)
        let mut b_transition_painted: u32 = 0;  // count of PAINTED via accessor (real paint only)
        let mut b_transition_skipped: u32 = 0;  // FYS-bug-C skips
        let mut b_transition_unknown: u32 = 0;  // accessor None (env-disable scenario)
        let mut transitions_run: u32 = 0;
        let mut b_warm_us: u128 = 0;
        let mut b_resume_us: u128 = 0;
        let mut b_first_frame_us: u128 = 0;
        let mut last_errno = String::new();
        let mut b_open_errno = String::new();
        let mut max_paint_stall_us: u128 = 0;
        let mut a_paint_errs: u32 = 0;
        let mut transition_errs: u32 = 0;

        // 2026-06-17 M4 — page-evict instrumentation.
        // - max_majflt_delta_tick: largest single-tick ru_majflt
        //   delta over a transition tick (the key disambiguator;
        //   HIGH = page-evict, ~0 = pure-GPU drain).
        // - sum_majflt_delta_transition: total ru_majflt delta
        //   across the entire transition window.
        // - sum_minflt_delta_transition: ditto for minor faults
        //   (side channel — high minflt+low majflt = no disk).
        // - stalled_ticks_with_majflt: count of transition ticks
        //   that BOTH stalled (call_us > 500_000) AND had majflt
        //   delta >= 5 (the joint signature for page-evict).
        // - stalled_ticks_total: count of all ticks with
        //   call_us > 500_000 (the >500ms boundary matches the
        //   tail_diag_blit_flush threshold).
        // - vmrss/vmswap snapshots at transition entry + exit.
        let mut max_majflt_delta_tick: i64 = 0;
        let mut sum_majflt_delta_transition: i64 = 0;
        let mut sum_minflt_delta_transition: i64 = 0;
        let mut stalled_ticks_with_majflt: u32 = 0;
        let mut stalled_ticks_total: u32 = 0;
        let mut vmrss_at_transition_entry_kb: u64 = 0;
        let mut vmswap_at_transition_entry_kb: u64 = 0;
        let mut majflt_at_transition_entry: i64 = 0;
        let mut minflt_at_transition_entry: i64 = 0;
        let mut vmrss_at_transition_exit_kb: u64 = 0;
        let mut vmswap_at_transition_exit_kb: u64 = 0;
        let mut majflt_at_transition_exit: i64 = 0;
        let mut minflt_at_transition_exit: i64 = 0;

        let t_start = Instant::now();
        let b_open_deadline = t_start + Duration::from_millis(b_open_ms);

        const A_MIN_FRAMES_POST_TRANSITION: u32 = 30;
        const TICK_NS: u64 = 33_333_333;
        const TOTAL_DEADLINE_MS: u64 = 30_000;
        let total_deadline = t_start + Duration::from_millis(TOTAL_DEADLINE_MS);

        let mut tick: u32 = 0;
        let mut a_post_transition: u32 = 0;
        while Instant::now() < total_deadline {
            let tick_start = Instant::now();
            tick += 1;

            // Phase transitions.
            match phase {
                Phase::ASoloPreOpen => {
                    if Instant::now() >= b_open_deadline {
                        phase = Phase::BOpening;
                    }
                }
                Phase::BOpening => {
                    eprintln!(
                        "[m4] OPENING B at t={} ms a_pretransition={}",
                        t_start.elapsed().as_millis(), a_pretransition,
                    );
                    let t_b_open = Instant::now();
                    match video_decode::prime_video_decoder_for_preload(&dem_b, slide_b_id) {
                        Ok(sb) => {
                            state_b = Some(sb);
                            b_warm_us = t_b_open.elapsed().as_micros();
                            eprintln!(
                                "[m4] B primed warm_us={} parking for {} ms",
                                b_warm_us, park_ms,
                            );
                            park_until = Some(Instant::now() + Duration::from_millis(park_ms));
                            phase = Phase::BParked;
                        }
                        Err(e) => {
                            let s = format!("{e:#}");
                            b_open_errno = s.clone();
                            eprintln!("[m4] B prime FAILED: {s}");
                            phase = Phase::Drain;
                        }
                    }
                }
                Phase::BParked => {
                    if let Some(p_until) = park_until {
                        if Instant::now() >= p_until {
                            // Sample memory + faults at transition
                            // entry (right BEFORE the first tick of
                            // paint_and_present_one_transition_frame).
                            let (rss, swap) = read_vmrss_vmswap_kb();
                            let majflt = read_majflt().unwrap_or(-1);
                            let minflt = read_minflt().unwrap_or(-1);
                            vmrss_at_transition_entry_kb = rss;
                            vmswap_at_transition_entry_kb = swap;
                            majflt_at_transition_entry = majflt;
                            minflt_at_transition_entry = minflt;
                            eprintln!(
                                "[m4] B PARK end → TRANSITION at t={} ms \
                                 vmrss_kb={} vmswap_kb={} majflt={} minflt={}",
                                t_start.elapsed().as_millis(),
                                rss, swap, majflt, minflt,
                            );
                            transition_start = Some(Instant::now());
                            transition_tick_idx = 0;
                            phase = Phase::Transition;
                        }
                    }
                }
                Phase::Transition => {
                    if transition_tick_idx >= transition_ticks {
                        transitions_run += 1;
                        // Sample memory + faults at transition exit
                        // (after the LAST tick has run). Pair with
                        // the entry sample to bracket the working-
                        // set + fault delta across the whole window.
                        let (rss, swap) = read_vmrss_vmswap_kb();
                        let majflt = read_majflt().unwrap_or(-1);
                        let minflt = read_minflt().unwrap_or(-1);
                        vmrss_at_transition_exit_kb = rss;
                        vmswap_at_transition_exit_kb = swap;
                        majflt_at_transition_exit = majflt;
                        minflt_at_transition_exit = minflt;
                        eprintln!(
                            "[m4] TRANSITION end ticks={} a_pretransition={} \
                             b_transition_frames={} \
                             vmrss_kb={} vmswap_kb={} majflt={} minflt={}",
                            transition_tick_idx, a_pretransition, b_transition_frames,
                            rss, swap, majflt, minflt,
                        );
                        phase = Phase::Drain;
                    }
                }
                Phase::Drain => {
                    if a_post_transition >= A_MIN_FRAMES_POST_TRANSITION {
                        break;
                    }
                }
            }

            // Per-phase paint.
            let t_paint = Instant::now();
            let phase_label = phase.as_str();

            match phase {
                Phase::ASoloPreOpen | Phase::BOpening | Phase::BParked | Phase::Drain => {
                    // A solo via M2's β-surface path. Window FB; swap
                    // is the tile-store barrier. Mirrors the steady-
                    // state PaintSlide path verified HEALTHY by M2 v3.
                    let n_a = dem_a.samples.len();
                    if state_a.next_sample_idx >= n_a {
                        state_a.next_sample_idx = 0;
                    }
                    let res = unsafe {
                        hdmi::bake_video_slide_to_current_fbo(
                            session,
                            &dem_a.samples,
                            &mut state_a.next_sample_idx,
                            &mut state_a.frames_decoded,
                            &state_a.decoder,
                            mode_w,
                            mode_h,
                            /* is_offscreen_bake */ false,
                        )
                    };
                    let painted = match res {
                        Ok(Some(_)) => {
                            a_pretransition += 1;
                            if matches!(phase, Phase::Drain) {
                                a_post_transition += 1;
                            }
                            true
                        }
                        Ok(None) => false,
                        Err(e) => {
                            let s = format!("{e:#}");
                            a_paint_errs += 1;
                            if last_errno.is_empty() {
                                last_errno = s.clone();
                            }
                            eprintln!("[m4] solo-A bake err: {s}");
                            false
                        }
                    };
                    // Solo-A screen oracle (catches regressions vs M2's
                    // proven HEALTHY baseline).
                    if painted {
                        let probe_w: u32 = 64u32.min(mode_w / 4);
                        let probe_h: u32 = 64u32.min(mode_h / 4);
                        let cx = (mode_w / 2).saturating_sub(probe_w / 2);
                        let cy = (mode_h / 2).saturating_sub(probe_h / 2);
                        let buf = read_back_buffer_rgba(session.gl(), cx, cy, probe_w, probe_h);
                        a_solo_screen_oracle.check(&buf);
                    }
                    if let Err(e) = hdmi::finish_video_slide_swap_and_commit(session, card) {
                        let s = format!("{e:#}");
                        eprintln!("[m4] solo-A swap err: {s}");
                        if last_errno.is_empty() {
                            last_errno = s;
                        }
                    }
                }
                Phase::Transition => {
                    // Construct progress ramp. Transition fn auto-fires
                    // its built-in `transition_tex_probe` once at the
                    // first tick where progress crosses 0.4 — that's
                    // the FBO oracle for sides a + b. M3 doesn't have
                    // to drive that probe; it just lets the transition
                    // fn run.
                    let progress = if transition_ticks <= 1 {
                        1.0_f32
                    } else {
                        transition_tick_idx as f32 / (transition_ticks - 1) as f32
                    };

                    // Endpoints — both Video, mut-borrowing the state
                    // fields. Constructed fresh per tick so the borrows
                    // don't outlive the call.
                    let state_b_ref = state_b.as_mut().expect("state_b set before Transition");
                    let endpoint_a = hdmi::TransitionEndpoint::Video {
                        samples: &dem_a.samples,
                        next_sample_idx: &mut state_a.next_sample_idx,
                        frames_decoded: &mut state_a.frames_decoded,
                        decoder: &state_a.decoder,
                    };
                    let endpoint_b = hdmi::TransitionEndpoint::Video {
                        samples: &dem_b.samples,
                        next_sample_idx: &mut state_b_ref.next_sample_idx,
                        frames_decoded: &mut state_b_ref.frames_decoded,
                        decoder: &state_b_ref.decoder,
                    };

                    // 2026-06-17 M4 — sample page faults BEFORE the
                    // transition call. Pairs with the post-call
                    // sample below for the per-tick delta. Cost is
                    // ~1 µs per getrusage() syscall — negligible.
                    //
                    // SACRED REVIEW (2026-06-17): read_majflt /
                    // read_minflt return Option<i64> — None on
                    // getrusage failure. The page-evict verdict
                    // hinges on the delta sum + max, so a single
                    // failed sample MUST NOT corrupt them. If either
                    // pre or post is None, skip the stat update for
                    // that tick (we still call the transition fn so
                    // the test runs; the stalled-with-majflt counter
                    // stays at its prior value).
                    let majflt_pre_opt = read_majflt();
                    let minflt_pre_opt = read_minflt();

                    let t_call = Instant::now();
                    let res = hdmi::paint_and_present_one_transition_frame(
                        session, card,
                        endpoint_a, endpoint_b,
                        None, None,
                        transition_kind, progress,
                    );
                    let call_us = t_call.elapsed().as_micros();
                    if call_us > max_paint_stall_us {
                        max_paint_stall_us = call_us;
                    }

                    let majflt_post_opt = read_majflt();
                    let minflt_post_opt = read_minflt();

                    // Only update the verdict-relevant counters when
                    // BOTH samples on each axis succeeded. minflt is
                    // an independent side channel from majflt.
                    let mut majflt_delta_str = String::from("FAIL");
                    let mut minflt_delta_str = String::from("FAIL");
                    let majflt_delta_for_stall_check: i64 =
                        match (majflt_pre_opt, majflt_post_opt) {
                            (Some(pre), Some(post)) => {
                                let delta = post - pre;
                                majflt_delta_str = delta.to_string();
                                if delta > max_majflt_delta_tick {
                                    max_majflt_delta_tick = delta;
                                }
                                sum_majflt_delta_transition += delta;
                                delta
                            }
                            _ => 0,
                        };
                    if let (Some(pre), Some(post)) = (minflt_pre_opt, minflt_post_opt) {
                        let delta = post - pre;
                        minflt_delta_str = delta.to_string();
                        sum_minflt_delta_transition += delta;
                    }

                    // Stalled-tick accounting. >500ms boundary matches
                    // tail_diag_blit_flush's threshold (hdmi.rs:9162),
                    // so a tick crossing this also auto-trips that
                    // marker in the journal — useful cross-reference.
                    // stalled_ticks_with_majflt only fires when we
                    // have a real majflt delta (not the failure-
                    // fallback 0) to avoid false-negative wedging.
                    if call_us > 500_000 {
                        stalled_ticks_total += 1;
                        if majflt_pre_opt.is_some() && majflt_post_opt.is_some()
                            && majflt_delta_for_stall_check >= 5
                        {
                            stalled_ticks_with_majflt += 1;
                        }
                    }

                    eprintln!(
                        "[m4] tick_paint progress={:.3} call_us={} \
                         majflt_pre={} majflt_delta={} minflt_delta={}",
                        progress, call_us,
                        majflt_pre_opt.map(|v| v.to_string())
                            .unwrap_or_else(|| String::from("FAIL")),
                        majflt_delta_str,
                        minflt_delta_str,
                    );

                    // 2026-06-17 — MAJOR #2 fix. Read the env-gated
                    // outcome tag emitted by paint_and_present_one_
                    // transition_frame's pre-swap probe. PAINTED =
                    // real composite presented; SKIPPED = FYS-bug-C
                    // no-frame-ready early-return (no swap, scanout
                    // holds prior frame); None = env unset (we auto-
                    // enable at startup so shouldn't fire unless QA
                    // explicitly sets =off).
                    let outcome = hdmi::take_last_transition_paint_outcome();
                    match res {
                        Ok(()) => {
                            b_transition_frames += 1;
                            match outcome {
                                Some("PAINTED") => {
                                    b_transition_painted += 1;
                                    // Only count first FRESH paint —
                                    // PAINTED guarantees the swap
                                    // ran AND the readback wasn't
                                    // intercepted by stale-reuse
                                    // (paint_transition_reuse_cached_b
                                    // still SWAPS; QA disambiguates
                                    // via stderr grep on that line).
                                    if b_first_frame_us == 0 {
                                        if let Some(t) = transition_start {
                                            b_first_frame_us = t.elapsed().as_micros();
                                            b_resume_us = b_first_frame_us;
                                        }
                                    }
                                }
                                Some("SKIPPED") => {
                                    b_transition_skipped += 1;
                                }
                                Some(_) | None => {
                                    b_transition_unknown += 1;
                                }
                            }
                        }
                        Err(e) => {
                            let s = format!("{e:#}");
                            transition_errs += 1;
                            if last_errno.is_empty() {
                                last_errno = s.clone();
                            }
                            eprintln!(
                                "[m4] transition tick={} progress={:.3} err: {s}",
                                transition_tick_idx, progress,
                            );
                        }
                    }

                    // 2026-06-17 — CRITICAL #1 fix from sacred
                    // review. The previous version did a glReadPixels
                    // on BACK AFTER paint_and_present_one_transition_
                    // frame returned, but that fn's internal
                    // eglSwapBuffers (hdmi.rs:5760) moves the just-
                    // composited content to FRONT and gives us a
                    // fresh undefined BO as BACK → readback was
                    // garbage → false DIVERGENT verdicts on healthy
                    // systems. The fix lives in the prod fn itself
                    // (env-gated PRE-swap probe at hdmi.rs:5757,
                    // emitting `[perf] transition_screen_oracle ...
                    // tag=PAINTED luma=N all_constant=Y`) which QA
                    // greps from the journal. The accessor read
                    // above already classified PAINTED vs SKIPPED
                    // for M3's summary counters.
                    eprintln!(
                        "[m4] transition tick={} progress={:.3} call_us={} \
                         outcome={:?}",
                        transition_tick_idx, progress, call_us,
                        outcome.unwrap_or("UNSET"),
                    );

                    transition_tick_idx += 1;
                }
            }

            let paint_us = t_paint.elapsed().as_micros();
            if paint_us > max_paint_stall_us {
                max_paint_stall_us = paint_us;
            }

            println!(
                "[m4] tick={tick} phase={phase_label} a_pretransition={a_pretransition} \
                 b_transition_frames={b_transition_frames} \
                 a_post_transition={a_post_transition} paint_us={paint_us}",
            );

            let elapsed = tick_start.elapsed().as_nanos() as u64;
            if elapsed < TICK_NS {
                std::thread::sleep(Duration::from_nanos(TICK_NS - elapsed));
            }
        }

        // ---------- VERDICT --------------------------------------------
        //
        // M3's in-process verdict uses the PAINTED/SKIPPED/unknown
        // counters from the env-gated accessor. Failure signatures
        // #1 (offscreen FBO black) and #3 (BLEND-PASS BLACK) live in
        // the journal's `[perf] transition_tex_probe` and `[perf]
        // transition_screen_oracle` lines — QA's grep job, not M3's
        // VERDICT to assess. M3's job here is to confirm "the
        // transition ran end-to-end + the painted-vs-skipped ratio
        // looks sensible" so QA knows whether to look at the journal
        // for r76 signatures vs to chase a wedge.
        // M4 page-evict disambiguation (per QA dispatch
        // 2026-06-17 post-M3 sweep):
        //   - max_majflt_delta_tick >= 50 over a stalled tick →
        //     PAGE_EVICT_PROBABLE (footprint reduction fixes it).
        //   - max_majflt_delta_tick <= 5 + max_paint_stall_us >
        //     1_000_000 (= 1 second) → PURE_GPU_DRAIN_PROBABLE
        //     (need a GPU/barrier fix in addition to footprint).
        //   - 6..=49 over a stalled tick → MIXED (both arcs
        //     contributing).
        //   - No multi-second stall observed → NO_FREEZE_OBSERVED
        //     (run was probabilistic miss; re-run).
        let pageevict_verdict: &'static str = if max_paint_stall_us < 1_000_000 {
            "NO_FREEZE_OBSERVED"
        } else if max_majflt_delta_tick >= 50 {
            "PAGE_EVICT_PROBABLE"
        } else if max_majflt_delta_tick <= 5 {
            "PURE_GPU_DRAIN_PROBABLE"
        } else {
            "MIXED"
        };

        let verdict = if !b_open_errno.is_empty() {
            "WEDGED"
        } else if transitions_run == 0 {
            "WEDGED"
        } else if transition_errs > 0 {
            "WEDGED"
        } else if b_transition_painted == 0 && b_transition_skipped >= 5 {
            // All ticks hit FYS-bug-C skip → decoder didn't produce
            // for any tick → wedge.
            "WEDGED"
        } else if b_transition_painted >= transition_ticks.saturating_sub(3)
            && a_paint_errs == 0
        {
            // Most ticks painted (real composite presented). DIVERGENT
            // vs HEALTHY then depends on the journal's transition_
            // screen_oracle + transition_tex_probe + paint_transition_
            // reuse_cached_b lines, which QA assesses.
            "HEALTHY"
        } else if b_transition_painted >= 10 {
            "DEGRADED"
        } else {
            "WEDGED"
        };

        println!(
            "[m4] PARK_MS={park_ms} B_OPEN_MS={b_open_ms} \
             a_warm_us={a_warm_us} a_pretransition={a_pretransition} \
             a_paint_errs={a_paint_errs} {a_solo_report} \
             b_warm_us={b_warm_us} b_resume_us={b_resume_us} \
             b_first_frame_us={b_first_frame_us} \
             transitions_run={transitions_run} \
             b_transition_frames={b_transition_frames}/{target} \
             b_transition_painted={b_transition_painted} \
             b_transition_skipped={b_transition_skipped} \
             b_transition_unknown={b_transition_unknown} \
             transition_errs={transition_errs} \
             transition_kind=\"{transition_kind}\" \
             offscreen_flush={offscreen_flush_env} \
             bake_b_poll_deadline_ms={bake_b_poll_deadline_ms} \
             b_open_errno=\"{b_open_errno}\" \
             last_errno=\"{last_errno}\" \
             paint_stall_us={paint_stall} \
             max_majflt_delta_tick={max_majflt_delta_tick} \
             sum_majflt_delta_transition={sum_majflt_delta_transition} \
             sum_minflt_delta_transition={sum_minflt_delta_transition} \
             stalled_ticks_total={stalled_ticks_total} \
             stalled_ticks_with_majflt={stalled_ticks_with_majflt} \
             vmrss_entry_kb={vmrss_entry_kb} vmrss_exit_kb={vmrss_exit_kb} \
             vmrss_delta_kb={vmrss_delta_kb} \
             vmswap_entry_kb={vmswap_entry_kb} vmswap_exit_kb={vmswap_exit_kb} \
             vmswap_delta_kb={vmswap_delta_kb} \
             majflt_entry={majflt_entry} majflt_exit={majflt_exit} \
             majflt_delta_transition={majflt_delta_transition} \
             minflt_entry={minflt_entry} minflt_exit={minflt_exit} \
             pageevict_verdict={pageevict_verdict} \
             VERDICT={verdict}",
            park_ms = park_ms,
            b_open_ms = b_open_ms,
            a_warm_us = a_warm_us,
            a_pretransition = a_pretransition,
            a_paint_errs = a_paint_errs,
            a_solo_report = a_solo_screen_oracle.report("a_solo_screen"),
            b_warm_us = b_warm_us,
            b_resume_us = b_resume_us,
            b_first_frame_us = b_first_frame_us,
            transitions_run = transitions_run,
            b_transition_frames = b_transition_frames,
            target = transition_ticks,
            b_transition_painted = b_transition_painted,
            b_transition_skipped = b_transition_skipped,
            b_transition_unknown = b_transition_unknown,
            transition_errs = transition_errs,
            transition_kind = transition_kind,
            offscreen_flush_env = offscreen_flush_env,
            bake_b_poll_deadline_ms = bake_b_poll_deadline_ms,
            b_open_errno = b_open_errno,
            last_errno = last_errno,
            paint_stall = max_paint_stall_us,
            max_majflt_delta_tick = max_majflt_delta_tick,
            sum_majflt_delta_transition = sum_majflt_delta_transition,
            sum_minflt_delta_transition = sum_minflt_delta_transition,
            stalled_ticks_total = stalled_ticks_total,
            stalled_ticks_with_majflt = stalled_ticks_with_majflt,
            vmrss_entry_kb = vmrss_at_transition_entry_kb,
            vmrss_exit_kb = vmrss_at_transition_exit_kb,
            vmrss_delta_kb = (vmrss_at_transition_exit_kb as i64)
                - (vmrss_at_transition_entry_kb as i64),
            vmswap_entry_kb = vmswap_at_transition_entry_kb,
            vmswap_exit_kb = vmswap_at_transition_exit_kb,
            vmswap_delta_kb = (vmswap_at_transition_exit_kb as i64)
                - (vmswap_at_transition_entry_kb as i64),
            majflt_entry = majflt_at_transition_entry,
            majflt_exit = majflt_at_transition_exit,
            majflt_delta_transition = majflt_at_transition_exit
                - majflt_at_transition_entry,
            minflt_entry = minflt_at_transition_entry,
            minflt_exit = minflt_at_transition_exit,
            pageevict_verdict = pageevict_verdict,
            verdict = verdict,
        );

        Ok(())
    }

    /// Mirror of M2's read_back_buffer_rgba — reads a tile from the
    /// default fb's BACK buffer as RGBA bytes. PixelOracle hashes the
    /// byte slice (FNV-1a) + all-constant detector, so RGBA tiles work
    /// the same way Y-plane bytes did. Kept for the solo-A phases
    /// (PreOpen/BOpening/BParked/Drain) where M2's read-BEFORE-swap
    /// pattern is correct and the read isn't intercepted by an
    /// internal swap.
    #[allow(dead_code)]
    fn read_back_buffer_rgba(
        gl: &glow::Context,
        x: u32,
        y: u32,
        w: u32,
        h: u32,
    ) -> Vec<u8> {
        use glow::HasContext;
        let mut buf = vec![0u8; (w as usize) * (h as usize) * 4];
        unsafe {
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            gl.read_buffer(glow::BACK);
            gl.pixel_store_i32(glow::PACK_ALIGNMENT, 1);
            gl.read_pixels(
                x as i32, y as i32, w as i32, h as i32,
                glow::RGBA, glow::UNSIGNED_BYTE,
                glow::PixelPackData::Slice(&mut buf[..]),
            );
        }
        buf
    }
}
