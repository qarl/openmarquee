//! M2 — display-path probe (Phase 2: lib-restructure variant, β surface).
//!
//! ## Why this version exists
//!
//! M2 v1 (`27ed869`) + v2 (`8a20145`) hand-wired their own EGL bring-up +
//! their own NV12 DMABUF blit shader + their own swap/present dance, on
//! the theory that lifting just-the-EGL-context-body would preserve
//! prod's GL state precisely enough to catch r76's "decoder-OK +
//! screen-BLACK" divergence. Both reproductions failed in the SAME way:
//! all 5 parks DIVERGENT, A all-black on screen even in the PREOPEN
//! phase (A painting alone, no contention). That's a wiring bug, not
//! the r76 freeze — prod paints single videos to the glass fine every
//! day.
//!
//! Per QA's call (2026-06-17): stop reinventing the display path. Pivot
//! to Option A — expose the renderer as a library so M2 can call the
//! REAL `run_in_egl_session` + `bake_video_slide_to_current_fbo` +
//! `finish_video_slide_swap_and_commit` instead of hand-wired copies
//! that are blindly missing some load-bearing piece of prod's setup.
//!
//! Phase 1 (commit `28b06b7`) shipped the lib.rs scaffolding +
//! cross-build verification. Phase 2 (this commit) rewires M2 onto the
//! real path.
//!
//! ## Lifted bodies → REAL fn calls (delta from v2)
//!
//! - Hand-wired EGL bring-up (`with_egl_session` body, ~140 LOC) →
//!   `openmarquee_render::hdmi::run_in_egl_session(&card, 0, |session| { ... })`.
//! - Hand-wired NV12 DMABUF blit + shader compile + EGLImage import
//!   (~270 LOC inside `linux_main`) →
//!   `unsafe { openmarquee_render::hdmi::bake_video_slide_to_current_fbo(session, samples, &mut next_sample_idx, &mut frames_decoded, decoder, mode_w, mode_h, false) }`.
//! - Hand-wired swap path (eglSwapBuffers + lock_front_buffer +
//!   add_framebuffer + set_crtc) →
//!   `openmarquee_render::hdmi::finish_video_slide_swap_and_commit(session, &card)`.
//! - Hand-wired `Card`, `GbmFb`, `DmaBufEglEps`, `resolve_dma_buf_egl_eps`,
//!   `compile_shader`, `build_nv12_blit_program`, `full_quad_vbo`,
//!   `blit_nv12_dmabuf_to_viewport` — all deleted; the lib provides
//!   the equivalents via `hdmi::*`.
//!
//! Net: probe shrinks from ~1041 LOC to ~250 LOC. Display-path
//! correctness is now prod's responsibility, not the probe's.
//!
//! ## β surface (visibility flips in hdmi.rs)
//!
//! Confirmed by QA at 2026-06-17 dispatch:
//! 1. `bake_video_slide_to_current_fbo` — `unsafe fn` → `pub unsafe fn`.
//! 2. `commit_fb` — `fn` → `pub fn`.
//! 3. `finish_video_slide_swap_and_commit` — `fn` → `pub fn`.
//! 4. `EglSession::mode_w()` / `EglSession::mode_h()` — new `pub fn`
//!    read-only accessors (the bake fn takes them as args).
//! 5. `EglSession::gl()` — already pub (pre-existing).
//!
//! All are behavior-neutral visibility flips for prod; the main binary
//! still calls the same fns through the same paths.
//!
//! ## Q3 dual-bake adjustment (with the real bake fn)
//!
//! QA's Q3 from the M2 dispatch: BAKE BOTH A AND B PER TICK during the
//! resume window, with screen oracle on BOTH A's region AND B's region.
//!
//! The real `bake_video_slide_to_current_fbo` sets glViewport(0, 0,
//! mode_w, mode_h) internally — it's designed for full-screen paint to
//! the window FBO. So instead of split-viewport compositing (would
//! require adding a viewport_x/y param to the prod bake fn → guard-#3
//! drift), M2 leverages READ ORDERING within the tick:
//!
//!   1. bake A full-screen into window FBO
//!   2. glReadPixels at A's region (left-half center) → A_screen oracle
//!      captures A's bake output BEFORE B's bake overdraws
//!   3. bake B full-screen into window FBO (overdraws A in framebuffer)
//!   4. glReadPixels at B's region (right-half center) → B_screen
//!      oracle captures B's bake output
//!   5. finish_video_slide_swap_and_commit (one swap per tick)
//!
//! The dual-bake-per-tick stress is preserved: both bakes through real
//! V4L2 → EGLImage → external-OES path, both on the single paint
//! thread, both into the same window FBO, both before the same
//! eglSwapBuffers barrier. The screen the operator's TV would show is
//! B's bake (last paint wins on the window FBO before swap), but for
//! a probe binary that's fine — what matters is the OUT-OF-BAND oracle
//! readback per side, which captures each bake's actual output.
//!
//! ## r76 fingerprint with the real bake
//!
//! `bake_video_slide_to_current_fbo` returns:
//!   - `Ok(None)`           — no fresh frame this tick (V4L2 EAGAIN /
//!     decoder starved; bake didn't paint).
//!   - `Ok(Some("DMABUF"))` — paint went through the zero-copy DMABUF
//!     EGLImage external-OES path.
//!   - `Ok(Some("MMAP"))`   — fell through to the MMAP upload path.
//!   - `Err(_)`             — hard error.
//!
//! The r76 freeze with REAL bake reproduces as:
//!   - bake returns `Ok(Some(_))` (claims it painted) AND
//!   - screen oracle for that side reads `all_constant` (black/solid)
//!     or `!distinct` (stuck on prior frame's pixels).
//!
//! This is exactly the iter-7 comment scenario at hdmi.rs:8584 (when
//! the tile-store happens after Frame::drop and the codec reclaims
//! the dma_buf before the deferred store executes) — but for the
//! WINDOW FBO path (`is_offscreen_bake=false`), where prod expects the
//! eglSwapBuffers barrier to force the tile-store correctly. If M2
//! still reproduces with the real bake on the window FBO, the freeze
//! lives somewhere this comment doesn't cover.
//!
//! If M2 is HEALTHY consistently with the real bake, the freeze isn't
//! reproducible in the simplified probe path — meaning the bug lives in
//! the transition_animated path (which uses is_offscreen_bake=true) or
//! some other paint path M2 doesn't exercise. That's also valuable
//! signal.
//!
//! ## Per-tick log + final VERDICT (M0/M1-style)
//!
//! Per-tick: `[m2] tick=N phase=... a_painted=Y/N a_path=DMABUF/MMAP/-
//! b_painted=Y/N b_path=DMABUF/MMAP/- a_scr_ok=Y/N b_scr_ok=Y/N
//! paint_us=N` so a sub-agent can replay the trajectory.
//!
//! Final VERDICT format (matches M0/M1 grep-uniform sweep):
//! `[m2] PARK_MS=N B_OPEN_MS=N a_painted=NN b_painted=NN/TARGET
//! a_scr_pixel_ok=NN/NN b_scr_pixel_ok=NN/NN screen_divergence=N
//! paint_stall_us=N a_black_during_b_bake=bool VERDICT=HEALTHY|DEGRADED|DIVERGENT|WEDGED`.
//!
//! ## Run recipe (fireplacesign, backend stopped, manual)
//!
//!   sudo systemctl stop openmarquee-backend
//!   for park_ms in 50 200 1000 5000 10000; do
//!     M0_PARK_MS=$park_ms \
//!     M1_VIDEO_A=/var/openmarquee/content/<uuidA>/asset.mp4 \
//!     M1_VIDEO_B=/var/openmarquee/content/<uuidB>/asset.mp4 \
//!     /usr/local/bin/m2-display-probe
//!   done
//!   sudo systemctl start openmarquee-backend  # restore
//!
//! Branch base: `task/frame-phase-instrument-2026-06-16` (Phase 1 lib
//! restructure at 28b06b7).

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("m2-display-probe: Linux-only (V4L2 + DRM/KMS + EGL).");
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
        BResuming,
        Drain,
    }

    impl Phase {
        fn as_str(&self) -> &'static str {
            match self {
                Phase::ASoloPreOpen => "preopen",
                Phase::BOpening => "opening",
                Phase::BParked => "parked",
                Phase::BResuming => "resuming",
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

        eprintln!(
            "[m2] params PARK_MS={park_ms} B_OPEN_MS={b_open_ms} A={video_a_path} B={video_b_path}",
        );

        // ---------- DRM device ----------------------------------------
        // Same fallback as prod (card0 → card1 if card0 fails).
        let card = Card::open(Path::new("/dev/dri/card0"))
            .or_else(|_| Card::open(Path::new("/dev/dri/card1")))
            .context("open /dev/dri/card{0,1}")?;

        // ---------- Demux + prime A (warm) ----------------------------
        let dem_a = mp4_demux::Mp4Demuxer::open(Path::new(&video_a_path))
            .with_context(|| format!("Mp4Demuxer::open A {video_a_path}"))?;
        let slide_a_id = uuid::Uuid::from_bytes([
            0x4d,0x32,0xaa,0xaa, 0,0,0,0, 0,0,0,0, 0,0,0,0,
        ]);
        let t_a_warm = Instant::now();
        let mut state_a = video_decode::prime_video_decoder_for_preload(&dem_a, slide_a_id)
            .map_err(|e| anyhow!("prime A: {e:#}"))?;
        let a_warm_us = t_a_warm.elapsed().as_micros();
        eprintln!(
            "[m2] A primed warm_us={} samples={} w={} h={}",
            a_warm_us, dem_a.samples.len(), dem_a.width, dem_a.height,
        );

        // ---------- Demux B (open deferred to BOpening phase) ----------
        let dem_b = mp4_demux::Mp4Demuxer::open(Path::new(&video_b_path))
            .with_context(|| format!("Mp4Demuxer::open B {video_b_path}"))?;
        let slide_b_id = uuid::Uuid::from_bytes([
            0x4d,0x32,0xbb,0xbb, 0,0,0,0, 0,0,0,0, 0,0,0,0,
        ]);
        eprintln!(
            "[m2] B mp4 parsed samples={} w={} h={}",
            dem_b.samples.len(), dem_b.width, dem_b.height,
        );

        // ---------- Drive everything inside the REAL EGL session --------
        // run_in_egl_session is the EXACT bring-up prod uses (same
        // DRM open, same GBM surface, same EGL config, same shaders'
        // setup hooks, same MakeCurrent). The closure body receives
        // the live &mut EglSession.
        hdmi::run_in_egl_session(&card, 0, |session| {
            tick_loop(
                session, &card,
                dem_a, &mut state_a, a_warm_us,
                dem_b, slide_b_id,
                park_ms, b_open_ms,
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
    ) -> Result<()> {
        use glow::HasContext;

        let mode_w = session.mode_w() as u32;
        let mode_h = session.mode_h() as u32;
        eprintln!("[m2] session up mode={}x{}", mode_w, mode_h);

        // ---------- Phase machine state -------------------------------
        let mut phase = Phase::ASoloPreOpen;
        let mut state_b: Option<video_decode::VideoDecoderState> = None;
        let mut park_until: Option<Instant> = None;
        let mut b_resume_start: Option<Instant> = None;

        // ---------- Oracles + counters --------------------------------
        // Screen oracles only — the real bake encapsulates feed +
        // dqbuf + paint, so the decoder Y-plane isn't exposed
        // out-of-band. The r76 fingerprint with the real bake is
        // bake-returns-Some-but-screen-reads-black; the decoder
        // oracle is folded into bake's return tag.
        let mut a_screen_oracle = probe_oracle::PixelOracle::new();
        let mut b_screen_oracle = probe_oracle::PixelOracle::new();

        let mut a_painted: u32 = 0;
        let mut b_painted: u32 = 0;
        let mut a_paint_errs: u32 = 0;
        let mut b_paint_errs: u32 = 0;
        let mut b_open_errno = String::new();
        let mut last_bake_errno = String::new();
        let mut b_warm_us: u128 = 0;
        let mut b_resume_us: u128 = 0;
        let mut b_first_resume_to_screen_us: u128 = 0;
        let mut a_painted_at_b_resume_start: u32 = 0;
        let mut a_painted_at_b_resume_end: u32 = 0;
        let mut a_black_count_during_b_bake: u32 = 0;
        let mut max_paint_stall_us: u128 = 0;

        let t_start = Instant::now();
        let b_open_deadline = t_start + Duration::from_millis(b_open_ms);

        const TARGET_B_FRAMES: u32 = 30;
        const A_MIN_FRAMES_POST_B_RESUME: u32 = 30;
        const TICK_NS: u64 = 33_333_333;
        const TOTAL_DEADLINE_MS: u64 = 25_000;
        let total_deadline = t_start + Duration::from_millis(TOTAL_DEADLINE_MS);

        let mut tick: u32 = 0;
        while Instant::now() < total_deadline {
            let tick_start = Instant::now();
            tick += 1;

            // -------- Phase transitions (open / park / resume / drain)
            // Decoder feed + dqbuf + paint is fully owned by the bake
            // call below; this block only mutates the phase state.
            match phase {
                Phase::ASoloPreOpen => {
                    if Instant::now() >= b_open_deadline {
                        phase = Phase::BOpening;
                    }
                }
                Phase::BOpening => {
                    eprintln!(
                        "[m2] OPENING B at t={} ms a_painted_so_far={}",
                        t_start.elapsed().as_millis(), a_painted,
                    );
                    let t_b_open = Instant::now();
                    match video_decode::prime_video_decoder_for_preload(&dem_b, slide_b_id) {
                        Ok(sb) => {
                            state_b = Some(sb);
                            b_warm_us = t_b_open.elapsed().as_micros();
                            eprintln!(
                                "[m2] B primed warm_us={} parking for {} ms",
                                b_warm_us, park_ms,
                            );
                            park_until = Some(Instant::now() + Duration::from_millis(park_ms));
                            phase = Phase::BParked;
                        }
                        Err(e) => {
                            let s = format!("{e:#}");
                            b_open_errno = s.clone();
                            eprintln!("[m2] B prime FAILED: {s}");
                            phase = Phase::Drain;
                        }
                    }
                }
                Phase::BParked => {
                    if let Some(p_until) = park_until {
                        if Instant::now() >= p_until {
                            eprintln!(
                                "[m2] B PARK end → RESUME at t={} ms",
                                t_start.elapsed().as_millis(),
                            );
                            b_resume_start = Some(Instant::now());
                            a_painted_at_b_resume_start = a_painted;
                            phase = Phase::BResuming;
                        }
                    }
                }
                Phase::BResuming => {
                    if let Some(state) = state_b.as_ref() {
                        let n_b = dem_b.samples.len();
                        // BResuming → Drain when B exhausts its
                        // sample list (advance_b's next_sample_idx
                        // reached n_b) AND has hit TARGET.
                        if state.next_sample_idx >= n_b && b_painted >= TARGET_B_FRAMES {
                            a_painted_at_b_resume_end = a_painted;
                            phase = Phase::Drain;
                        } else if b_painted >= TARGET_B_FRAMES {
                            a_painted_at_b_resume_end = a_painted;
                            phase = Phase::Drain;
                        }
                    }
                }
                Phase::Drain => {
                    let post = a_painted.saturating_sub(a_painted_at_b_resume_end);
                    if post >= A_MIN_FRAMES_POST_B_RESUME {
                        break;
                    }
                }
            }

            // Whether B should bake this tick. Resume/Drain only —
            // during BParked, B's decoder must NOT advance (the whole
            // point of the warm-park-resume profile).
            let b_active = matches!(phase, Phase::BResuming | Phase::Drain)
                && state_b.is_some();

            // ---- Paint pass --------------------------------------------
            let t_paint = Instant::now();

            // Clear back buffer to known-black so screen oracle can
            // distinguish "bake never painted" (all-black) from
            // "bake painted black pixels" (reproduces r76).
            unsafe {
                let gl = session.gl();
                gl.bind_framebuffer(glow::FRAMEBUFFER, None);
                gl.viewport(0, 0, mode_w as i32, mode_h as i32);
                gl.clear_color(0.0, 0.0, 0.0, 1.0);
                gl.clear(glow::COLOR_BUFFER_BIT);
                gl.disable(glow::DEPTH_TEST);
                gl.disable(glow::BLEND);
            }

            // Bake A (always — A is "live" throughout). M1-hygiene fix:
            // wrap next_sample_idx so A stays live past its sample
            // count through B's resume window.
            let n_a = dem_a.samples.len();
            if state_a.next_sample_idx >= n_a {
                state_a.next_sample_idx = 0;
            }
            let a_bake = unsafe {
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
            let mut a_painted_this_tick = false;
            let a_path_tag: &'static str = match a_bake {
                Ok(Some(tag)) => {
                    a_painted += 1;
                    a_painted_this_tick = true;
                    tag
                }
                Ok(None) => "-",
                Err(e) => {
                    let s = format!("{e:#}");
                    a_paint_errs += 1;
                    if last_bake_errno.is_empty() {
                        last_bake_errno = s.clone();
                    }
                    eprintln!("[m2] A bake err: {s}");
                    "ERR"
                }
            };

            // glReadPixels A's region BEFORE B's bake overdraws.
            // Read from the BACK buffer (default fb bound, pre-swap).
            // 64x64 sample at A's region (left-half center, no half
            // when B inactive — center of screen).
            let probe_w: u32 = 64u32.min(mode_w / 4);
            let probe_h: u32 = 64u32.min(mode_h / 4);
            let (a_px, a_py) = if b_active {
                ((mode_w / 4).saturating_sub(probe_w / 2), (mode_h / 2).saturating_sub(probe_h / 2))
            } else {
                ((mode_w / 2).saturating_sub(probe_w / 2), (mode_h / 2).saturating_sub(probe_h / 2))
            };
            // Only check A's screen oracle when A's bake claimed paint;
            // otherwise the back buffer is just the clear color (which
            // would always be all_constant and pollute the metric).
            if a_painted_this_tick {
                let buf = read_back_buffer_rgba(session.gl(), a_px, a_py, probe_w, probe_h);
                let v = a_screen_oracle.check(&buf);
                // r76 secondary signal: A going black while B is also
                // baking. Track only when B is mid-bake on the same
                // tick (so we can distinguish "A bake itself went
                // black" from "A's pixels were correctly painted but
                // got overdrawn by B before our read" — which would
                // be a bug in OUR read ordering, not r76).
                if b_active && v.all_constant {
                    a_black_count_during_b_bake += 1;
                }
            }

            // Bake B (resume window only). Full-screen overdraw of A
            // on the window FBO. Both bakes hit the same window FBO
            // before any swap — this is the dual-bake-per-tick stress.
            let mut b_path_tag: &'static str = "-";
            if b_active {
                let state = state_b.as_mut().unwrap();
                let n_b = dem_b.samples.len();
                if state.next_sample_idx < n_b {
                    let b_bake = unsafe {
                        hdmi::bake_video_slide_to_current_fbo(
                            session,
                            &dem_b.samples,
                            &mut state.next_sample_idx,
                            &mut state.frames_decoded,
                            &state.decoder,
                            mode_w,
                            mode_h,
                            /* is_offscreen_bake */ false,
                        )
                    };
                    match b_bake {
                        Ok(Some(tag)) => {
                            if b_painted == 0 {
                                b_resume_us = b_resume_start
                                    .map(|t| t.elapsed().as_micros()).unwrap_or(0);
                            }
                            b_painted += 1;
                            b_path_tag = tag;
                        }
                        Ok(None) => {
                            b_path_tag = "-";
                        }
                        Err(e) => {
                            let s = format!("{e:#}");
                            b_paint_errs += 1;
                            if last_bake_errno.is_empty() {
                                last_bake_errno = s.clone();
                            }
                            eprintln!("[m2] B bake err: {s}");
                            b_path_tag = "ERR";
                        }
                    }
                } else {
                    // B has exhausted its sample list (Drain phase).
                }

                // glReadPixels B's region AFTER B's bake.
                let bx = (3 * mode_w / 4).saturating_sub(probe_w / 2);
                let by = (mode_h / 2).saturating_sub(probe_h / 2);
                if b_path_tag != "-" && b_path_tag != "ERR" {
                    let buf = read_back_buffer_rgba(session.gl(), bx, by, probe_w, probe_h);
                    let v = b_screen_oracle.check(&buf);
                    if b_first_resume_to_screen_us == 0 && !v.all_constant {
                        b_first_resume_to_screen_us = b_resume_start
                            .map(|t| t.elapsed().as_micros()).unwrap_or(0);
                    }
                }
            }

            // Real swap path — same eglSwapBuffers + lock_front_buffer +
            // add_framebuffer + set_crtc/page_flip prod uses.
            if let Err(e) = hdmi::finish_video_slide_swap_and_commit(session, card) {
                let s = format!("{e:#}");
                eprintln!("[m2] swap err: {s}");
                if last_bake_errno.is_empty() {
                    last_bake_errno = s;
                }
            }

            let paint_us = t_paint.elapsed().as_micros();
            if paint_us > max_paint_stall_us {
                max_paint_stall_us = paint_us;
            }

            // Per-tick log line.
            println!(
                "[m2] tick={tick} phase={} a_path={} b_path={} a_painted={} b_painted={} paint_us={}",
                phase.as_str(), a_path_tag, b_path_tag, a_painted, b_painted, paint_us,
            );

            // 30 fps cadence.
            let elapsed = tick_start.elapsed().as_nanos() as u64;
            if elapsed < TICK_NS {
                std::thread::sleep(Duration::from_nanos(TICK_NS - elapsed));
            }
        }

        // ---------- VERDICT --------------------------------------------
        let a_during_b_resume =
            a_painted_at_b_resume_end.saturating_sub(a_painted_at_b_resume_start);
        // Screen divergence: bakes that claimed Some(_) but screen
        // oracle marked the read all_constant or stuck. probe_oracle
        // already counts `black` (all_constant) and `stuck` (not
        // distinct).
        let a_screen_total = a_screen_oracle.total;
        let b_screen_pixel_ok = b_screen_oracle.pixel_ok;
        let b_screen_total = b_screen_oracle.total;
        let screen_divergence = b_painted.saturating_sub(b_screen_pixel_ok);

        let verdict = if !b_open_errno.is_empty() || a_paint_errs > 0 {
            "WEDGED"
        } else if b_painted == 0 {
            "WEDGED"
        } else if screen_divergence >= 3 {
            // r76 fingerprint: bake claimed Some(_) but screen oracle
            // saw black/stuck pixels on B's region.
            "DIVERGENT"
        } else if b_painted >= TARGET_B_FRAMES
            && b_screen_pixel_ok >= b_painted.saturating_sub(2)
            && a_during_b_resume >= 10
            && b_paint_errs == 0
        {
            "HEALTHY"
        } else if b_painted >= 15 && b_screen_pixel_ok >= 10 {
            "DEGRADED"
        } else {
            "WEDGED"
        };

        println!(
            "[m2] PARK_MS={park_ms} B_OPEN_MS={b_open_ms} \
             a_warm_us={a_warm_us} a_painted={a_painted} a_paint_errs={a_paint_errs} \
             {a_scr_report} \
             b_warm_us={b_warm_us} b_resume_us={b_resume_us} \
             b_first_resume_to_screen_us={b_first_resume_to_screen_us} \
             b_painted={b_painted}/{target} b_paint_errs={b_paint_errs} \
             {b_scr_report} \
             a_during_b_resume={a_during_b_resume} \
             b_open_errno=\"{b_open_errno}\" \
             last_bake_errno=\"{last_bake_errno}\" \
             paint_stall_us={paint_stall} \
             a_black_during_b_bake={a_black_during_b_bake} \
             a_screen_total={a_screen_total} b_screen_total={b_screen_total} \
             screen_divergence={screen_divergence} \
             VERDICT={verdict}",
            park_ms = park_ms,
            b_open_ms = b_open_ms,
            a_warm_us = a_warm_us,
            a_painted = a_painted,
            a_paint_errs = a_paint_errs,
            a_scr_report = a_screen_oracle.report("a_screen"),
            b_warm_us = b_warm_us,
            b_resume_us = b_resume_us,
            b_first_resume_to_screen_us = b_first_resume_to_screen_us,
            b_painted = b_painted,
            target = TARGET_B_FRAMES,
            b_paint_errs = b_paint_errs,
            b_scr_report = b_screen_oracle.report("b_screen"),
            a_during_b_resume = a_during_b_resume,
            b_open_errno = b_open_errno,
            last_bake_errno = last_bake_errno,
            paint_stall = max_paint_stall_us,
            a_black_during_b_bake = a_black_count_during_b_bake,
            a_screen_total = a_screen_total,
            b_screen_total = b_screen_total,
            screen_divergence = screen_divergence,
            verdict = verdict,
        );

        Ok(())
    }

    /// Read a tile from the back buffer (default fb) as RGBA bytes.
    /// The buffer can be hashed/inspected by PixelOracle directly —
    /// it does FNV-1a over the byte slice and an all-constant
    /// detector; works on RGBA tiles the same way it worked on
    /// Y-plane bytes.
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
