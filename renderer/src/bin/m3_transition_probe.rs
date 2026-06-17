//! M3 — transition-path probe: drive the REAL cross-fade transition.
//!
//! ## Where M3 lives in the M0/M1/M2/M3 chain
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
//! `[m3] PARK_MS=N B_OPEN_MS=N a_warm_us=N b_warm_us=N b_resume_us=N
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
    eprintln!("m3-transition-probe: Linux-only (V4L2 + DRM/KMS + EGL).");
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
            "[m3] params PARK_MS={park_ms} B_OPEN_MS={b_open_ms} KIND={transition_kind} \
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
            "[m3] A primed warm_us={} samples={} w={} h={}",
            a_warm_us, dem_a.samples.len(), dem_a.width, dem_a.height,
        );

        // ---------- Demux B (open deferred) ---------------------------
        let dem_b = mp4_demux::Mp4Demuxer::open(Path::new(&video_b_path))
            .with_context(|| format!("Mp4Demuxer::open B {video_b_path}"))?;
        let slide_b_id = uuid::Uuid::from_bytes([
            0x4d,0x33,0xbb,0xbb, 0,0,0,0, 0,0,0,0, 0,0,0,0,
        ]);
        eprintln!(
            "[m3] B mp4 parsed samples={} w={} h={}",
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
        eprintln!("[m3] session up mode={}x{}", mode_w, mode_h);

        let mut phase = Phase::ASoloPreOpen;
        let mut state_b: Option<video_decode::VideoDecoderState> = None;
        let mut park_until: Option<Instant> = None;
        let mut transition_start: Option<Instant> = None;
        let mut transition_tick_idx: u32 = 0;

        // Oracles + counters.
        // M3's PRIMARY screen oracle: the window-fb readback at the
        // end of each transition tick (B's contribution as composited
        // by FS_FADE). Per dispatch: this catches BLEND-PASS BLACK if
        // both tex_a + tex_b are non-zero but the final composite is
        // black.
        let mut final_screen_oracle = probe_oracle::PixelOracle::new();
        let mut a_solo_screen_oracle = probe_oracle::PixelOracle::new();

        let mut a_pretransition: u32 = 0;
        let mut b_transition_frames: u32 = 0;
        let mut transitions_run: u32 = 0;
        let mut b_warm_us: u128 = 0;
        let mut b_resume_us: u128 = 0;
        let mut b_first_frame_us: u128 = 0;
        let mut last_errno = String::new();
        let mut b_open_errno = String::new();
        let mut max_paint_stall_us: u128 = 0;
        let mut a_paint_errs: u32 = 0;
        let mut transition_errs: u32 = 0;

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
                        "[m3] OPENING B at t={} ms a_pretransition={}",
                        t_start.elapsed().as_millis(), a_pretransition,
                    );
                    let t_b_open = Instant::now();
                    match video_decode::prime_video_decoder_for_preload(&dem_b, slide_b_id) {
                        Ok(sb) => {
                            state_b = Some(sb);
                            b_warm_us = t_b_open.elapsed().as_micros();
                            eprintln!(
                                "[m3] B primed warm_us={} parking for {} ms",
                                b_warm_us, park_ms,
                            );
                            park_until = Some(Instant::now() + Duration::from_millis(park_ms));
                            phase = Phase::BParked;
                        }
                        Err(e) => {
                            let s = format!("{e:#}");
                            b_open_errno = s.clone();
                            eprintln!("[m3] B prime FAILED: {s}");
                            phase = Phase::Drain;
                        }
                    }
                }
                Phase::BParked => {
                    if let Some(p_until) = park_until {
                        if Instant::now() >= p_until {
                            eprintln!(
                                "[m3] B PARK end → TRANSITION at t={} ms",
                                t_start.elapsed().as_millis(),
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
                        eprintln!(
                            "[m3] TRANSITION end ticks={} a_pretransition={} \
                             b_transition_frames={}",
                            transition_tick_idx, a_pretransition, b_transition_frames,
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
                            eprintln!("[m3] solo-A bake err: {s}");
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
                        eprintln!("[m3] solo-A swap err: {s}");
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

                    match res {
                        Ok(()) => {
                            b_transition_frames += 1;
                            if b_first_frame_us == 0 {
                                if let Some(t) = transition_start {
                                    b_first_frame_us = t.elapsed().as_micros();
                                }
                                if let Some(t) = transition_start {
                                    b_resume_us = t.elapsed().as_micros();
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
                                "[m3] transition tick={} progress={:.3} err: {s}",
                                transition_tick_idx, progress,
                            );
                        }
                    }

                    // Window screen oracle on the just-presented frame.
                    // The transition fn's commit_fb already swapped +
                    // page-flipped, but the BACK buffer holds what was
                    // just composited (post-swap, the "back" is the
                    // formerly-presented bo). Read center patch + a
                    // patch on B's half for asymmetric coverage.
                    {
                        let probe_w: u32 = 64u32.min(mode_w / 4);
                        let probe_h: u32 = 64u32.min(mode_h / 4);
                        let cx = (mode_w / 2).saturating_sub(probe_w / 2);
                        let cy = (mode_h / 2).saturating_sub(probe_h / 2);
                        let buf = read_back_buffer_rgba(session.gl(), cx, cy, probe_w, probe_h);
                        let v = final_screen_oracle.check(&buf);
                        eprintln!(
                            "[m3] transition tick={} progress={:.3} call_us={} screen_distinct={} screen_black={}",
                            transition_tick_idx, progress, call_us, v.distinct, v.all_constant,
                        );
                    }

                    transition_tick_idx += 1;
                }
            }

            let paint_us = t_paint.elapsed().as_micros();
            if paint_us > max_paint_stall_us {
                max_paint_stall_us = paint_us;
            }

            println!(
                "[m3] tick={tick} phase={phase_label} a_pretransition={a_pretransition} \
                 b_transition_frames={b_transition_frames} \
                 a_post_transition={a_post_transition} paint_us={paint_us}",
            );

            let elapsed = tick_start.elapsed().as_nanos() as u64;
            if elapsed < TICK_NS {
                std::thread::sleep(Duration::from_nanos(TICK_NS - elapsed));
            }
        }

        // ---------- VERDICT --------------------------------------------
        let final_screen_total = final_screen_oracle.total;
        let final_screen_pixel_ok = final_screen_oracle.pixel_ok;

        let verdict = if !b_open_errno.is_empty() {
            "WEDGED"
        } else if transitions_run == 0 {
            "WEDGED"
        } else if transition_errs > 0 {
            "WEDGED"
        } else if final_screen_oracle.stuck >= final_screen_total.saturating_sub(2)
            && final_screen_total >= 5
        {
            // Stale-buffer / blend-frozen reproduction.
            "DIVERGENT"
        } else if final_screen_oracle.black >= final_screen_total.saturating_sub(2)
            && final_screen_total >= 5
        {
            // BLEND-PASS BLACK or r76-proper offscreen FBO black
            // propagating to the composite.
            "DIVERGENT"
        } else if b_transition_frames >= transition_ticks.saturating_sub(2)
            && final_screen_pixel_ok >= final_screen_total.saturating_sub(2)
            && a_paint_errs == 0
        {
            "HEALTHY"
        } else if b_transition_frames >= 10 && final_screen_pixel_ok >= 5 {
            "DEGRADED"
        } else {
            "WEDGED"
        };

        println!(
            "[m3] PARK_MS={park_ms} B_OPEN_MS={b_open_ms} \
             a_warm_us={a_warm_us} a_pretransition={a_pretransition} \
             a_paint_errs={a_paint_errs} {a_solo_report} \
             b_warm_us={b_warm_us} b_resume_us={b_resume_us} \
             b_first_frame_us={b_first_frame_us} \
             transitions_run={transitions_run} \
             b_transition_frames={b_transition_frames}/{target} \
             transition_errs={transition_errs} \
             {final_report} \
             final_screen_pixel_ok={final_screen_pixel_ok}/{final_screen_total} \
             transition_kind=\"{transition_kind}\" \
             offscreen_flush={offscreen_flush_env} \
             bake_b_poll_deadline_ms={bake_b_poll_deadline_ms} \
             b_open_errno=\"{b_open_errno}\" \
             last_errno=\"{last_errno}\" \
             paint_stall_us={paint_stall} \
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
            transition_errs = transition_errs,
            final_report = final_screen_oracle.report("final_screen"),
            final_screen_pixel_ok = final_screen_pixel_ok,
            final_screen_total = final_screen_total,
            transition_kind = transition_kind,
            offscreen_flush_env = offscreen_flush_env,
            bake_b_poll_deadline_ms = bake_b_poll_deadline_ms,
            b_open_errno = b_open_errno,
            last_errno = last_errno,
            paint_stall = max_paint_stall_us,
            verdict = verdict,
        );

        Ok(())
    }

    /// Mirror of M2's read_back_buffer_rgba — reads a tile from the
    /// default fb's BACK buffer as RGBA bytes. PixelOracle hashes the
    /// byte slice (FNV-1a) + all-constant detector, so RGBA tiles work
    /// the same way Y-plane bytes did.
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
