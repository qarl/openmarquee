//! blendr — Phase 0: KMS-present milestone.
//!
//! See README.md and the per-module docs. This binary proves we
//! can drive HDMI from Rust on the Pi Zero 2 W (vc4 display +
//! V3D GPU) at 60Hz vsync, with clean restore-on-exit so the
//! screen doesn't stay black.

#![deny(unsafe_op_in_unsafe_fn)]
#![warn(rust_2018_idioms)]

use anyhow::{Context, Result};
use clap::Parser;
use std::path::PathBuf;

mod drm_probe;
mod egl_gbm;
mod gles_present;
mod gst_decode;
mod kms;
mod signals;

use gles_present::Step;

#[derive(Parser, Debug)]
#[command(name = "blendr", version, about = "Phase 0 KMS-present milestone")]
struct Args {
    /// How long to render before tearing down and restoring scanout.
    #[arg(long, default_value_t = 30)]
    duration_sec: u64,

    /// Which step to render.
    ///   solid   = hue-cycling clear color (Phase 0; proves swap+flip).
    ///   checker = static 256x256 checkerboard via GLES2 shader
    ///             (Phase 0; proves the GLES2 path is up too).
    ///   video   = Phase 1 KEYSTONE: one GStreamer pipeline decodes
    ///             --clip and hands frames to blendr as GL textures.
    ///   blend   = Phase 2: TWO pipelines (--clip-a + --clip-b),
    ///             mix(texA, texB, u_alpha) shader; --alpha controls
    ///             the static blend (default 0.5 = 50/50 ghosted).
    #[arg(long, value_enum, default_value_t = Step::Checker)]
    step: Step,

    /// Required when --step video. Absolute path to the H.264
    /// mp4 clip to decode. cutloop.py's content layout is
    /// /var/openmarquee/content/<uuid>/asset.mp4.
    #[arg(long)]
    clip: Option<PathBuf>,

    /// Phase 3 v2: comma-separated list of clip paths for the
    /// cycling playlist (arbitrary length N >= 1; full reel
    /// = all 21). Replaces --clip-a/--clip-b as the canonical
    /// form. For N=1, behaves like --step video. For N=2,
    /// behaves like Phase 3 v1 (both clips long-lived;
    /// alternates A<->B forever; no retire+recreate). For
    /// N>=3, the slot retire+recreate machinery cycles the
    /// 2-decoder pool through the full playlist.
    /// Example: --clips /var/openmarquee/content/aaa/asset.mp4,/var/openmarquee/content/bbb/asset.mp4,...
    #[arg(long, value_delimiter = ',')]
    clips: Vec<PathBuf>,

    /// 2-clip back-compat (Phase 3 v1 callers): first clip.
    /// If --clips is empty AND both --clip-a and --clip-b are
    /// set, desugars to --clips=A,B. New code should use
    /// --clips.
    #[arg(long)]
    clip_a: Option<PathBuf>,

    /// 2-clip back-compat: second clip. See --clip-a.
    #[arg(long)]
    clip_b: Option<PathBuf>,

    /// Phase 2 alpha (DEPRECATED -- replaced by --hold-sec +
    /// --crossfade-sec in Phase 3). Kept as a CLI alias for
    /// the previous static-alpha behavior: when --hold-sec is
    /// huge (e.g. 9999) the alpha stays at this value forever.
    /// Default 0.5 = 50/50 dissolve.
    #[arg(long, default_value_t = 0.5)]
    alpha: f32,

    /// Phase 3 slideshow hold duration. Each clip stays
    /// visible (alpha=0.0 for A, alpha=1.0 for B) for this
    /// many seconds before the crossfade fires. Default 20.0
    /// per qarl's "20s per video" rule. For testing the
    /// crossfade cadence in a bounded --duration-sec run,
    /// shorten this (e.g. --hold-sec 3 to exercise 7+ cycles
    /// in 60s).
    #[arg(long, default_value_t = 20.0)]
    hold_sec: f32,

    /// Phase 3 crossfade duration. Alpha ramps 0.0->1.0 (or
    /// 1.0->0.0) over this many seconds. Default 1.0 = 1s
    /// dissolve.
    #[arg(long, default_value_t = 1.0)]
    crossfade_sec: f32,

    /// Debug aid (per QA ask): freeze the Phase 3 slideshow
    /// at a fixed alpha. When set, the state machine bypasses
    /// phase transitions and returns this alpha forever, so
    /// --capture lands deterministically at a known blend
    /// ratio instead of the frame-number lottery. Range [0.0,
    /// 1.0]. Unset = normal animated slideshow.
    #[arg(long)]
    static_alpha: Option<f32>,

    /// Bypass /dev/dri/card* auto-probe. Use only for debug.
    #[arg(long)]
    card_override: Option<PathBuf>,

    /// Write one PPM (P6 binary) capture of the rendered back
    /// buffer to PATH on the Nth-frame draw (see
    /// --capture-after-frame). Lets QA verify actual pixels for
    /// the GL output without depending on qarl-eyes (kmsgrab hangs
    /// the live plane; the raw GBM/KMS path has no GST pixel-tee).
    /// PPM is chosen over PNG for zero added deps; QA converts
    /// with `magick foo.ppm foo.png` or `ffmpeg -i foo.ppm foo.png`.
    /// The render loop continues normally after the dump.
    #[arg(long)]
    capture: Option<PathBuf>,

    /// Which frame index to capture on (zero-based; 0 = the very
    /// first rendered frame). Default 30 gives ~half a second of
    /// runtime at 60Hz so the texture upload + first few flips
    /// have settled.
    #[arg(long, default_value_t = 30)]
    capture_after_frame: u64,
}

fn main() -> Result<()> {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info"))
        .format_timestamp_millis()
        .init();
    let args = Args::parse();
    log::info!(
        "[blendr] start duration={}s step={:?}",
        args.duration_sec,
        args.step
    );

    signals::install().context("signal handlers")?;
    run(args)
}

#[cfg(target_os = "linux")]
fn run(args: Args) -> Result<()> {
    use std::sync::Arc;

    let pick = drm_probe::pick_display_node(args.card_override.as_deref())
        .context("DRM node probe")?;
    log::info!(
        "[blendr] DRM node {} (driver={}) connector={:?}",
        pick.path.display(),
        pick.driver,
        pick.connector_id
    );
    let card = Arc::new(drm_probe::Card::open(&pick.path)?);
    let saved = kms::save_current_state(&card, pick.connector_id)
        .context("save_current_state")?;
    let mode_pick = kms::pick_connector_mode(&card, pick.connector_id)
        .context("pick_connector_mode")?;
    let mut gbm = egl_gbm::Gbm::new(&card, mode_pick.w, mode_pick.h)
        .context("GBM bring-up")?;
    let mut egl = egl_gbm::Egl::bring_up(&mut gbm).context("EGL bring-up")?;
    let mut pres = gles_present::Presenter::new(
        &egl,
        mode_pick.w,
        mode_pick.h,
        args.step,
    )
    .context("Presenter::new")?;

    // Phase 1 / Phase 2: build GstDecoders per step.
    let mut streams: kms::Streams = match args.step {
        Step::Video => {
            let clip = args.clip.as_deref().ok_or_else(|| {
                anyhow::anyhow!("--step video requires --clip <PATH>")
            })?;
            kms::Streams::Single(
                gst_decode::GstDecoder::new(&egl, clip)
                    .context("GstDecoder::new (video)")?,
            )
        }
        Step::Blend => {
            // Resolve playlist from --clips, falling back to
            // --clip-a/--clip-b for back-compat.
            let mut playlist: Vec<PathBuf> = args.clips.clone();
            if playlist.is_empty() {
                match (&args.clip_a, &args.clip_b) {
                    (Some(a), Some(b)) => {
                        playlist.push(a.clone());
                        playlist.push(b.clone());
                    }
                    _ => {
                        anyhow::bail!(
                            "--step blend requires either --clips A,B,C,... \
                             OR both --clip-a + --clip-b"
                        );
                    }
                }
            }
            let n = playlist.len();
            if n == 0 {
                anyhow::bail!("--step blend playlist is empty");
            }
            let basenames: Vec<&str> = playlist
                .iter()
                .map(|p| {
                    p.file_name()
                        .and_then(|s| s.to_str())
                        .unwrap_or("?")
                })
                .collect();
            log::info!(
                "[blendr] playlist N={n} clips={basenames:?}"
            );

            // N == 1: degenerate -- just play one clip. Use the
            // simpler Streams::Single path (no slideshow alpha).
            if n == 1 {
                let dec = gst_decode::GstDecoder::new(&egl, &playlist[0])
                    .context("GstDecoder::new (Phase 3 v2 N=1)")?;
                kms::Streams::Single(dec)
            } else {
                // N >= 2: build the Streams::Cycle variant.
                // Seed slot 0 with playlist[0] and slot 1 with
                // playlist[1]; next_idx points at the next clip
                // to load on the first retire+recreate. For N=2
                // next_idx is unused (no recreate fires).
                let dec0 = gst_decode::GstDecoder::new(&egl, &playlist[0])
                    .context("GstDecoder::new (slot 0)")?;
                let dec1 = gst_decode::GstDecoder::new(&egl, &playlist[1 % n])
                    .context("GstDecoder::new (slot 1)")?;
                let next_idx = 2 % n;
                let mut slideshow =
                    kms::SlideshowState::new(args.hold_sec, args.crossfade_sec);
                if let Some(alpha) = args.static_alpha {
                    let clamped = alpha.clamp(0.0, 1.0);
                    slideshow.set_static_alpha(clamped);
                    log::info!(
                        "[slideshow] starting: STATIC-ALPHA={} \
                         (hold_sec / crossfade_sec IGNORED)",
                        clamped,
                    );
                } else {
                    log::info!(
                        "[slideshow] starting: hold_sec={} crossfade_sec={} \
                         N={n} next_idx={next_idx} \
                         (--alpha={} unused; --static-alpha not set)",
                        args.hold_sec,
                        args.crossfade_sec,
                        args.alpha,
                    );
                }
                kms::Streams::Cycle {
                    playlist,
                    slots: [Some(dec0), Some(dec1)],
                    next_idx,
                    recreate_pending: false,
                    slideshow,
                    last_retire_at: None,
                    last_create_at: None,
                }
            }
        }
        _ => kms::Streams::None,
    };

    let run_result = kms::run_loop(
        &card,
        &mode_pick,
        &mut gbm,
        &mut egl,
        &mut pres,
        &mut streams,
        args.duration_sec,
        args.capture.as_deref(),
        args.capture_after_frame,
        &signals::EXIT_REQUESTED,
    );

    // BUG 4 fix: BEFORE drop(streams), reclaim EGL on the main
    // thread and release any GL bindings the presenter holds
    // into gst-managed textures. Without this, --capture runs
    // wedge at teardown (Phase 3 v1 glass: "[gst-pull] stop
    // flag set" logs then process spins on /dev/video10
    // forever). Also critical for Phase 3 v2's per-clip
    // retire+recreate path.
    if let Err(e) = egl.make_current() {
        log::warn!("[blendr] pre-teardown make_current failed: {e:#}");
    }
    pres.release_video_bindings();

    // LOAD-BEARING DROP ORDER:
    //   streams (each GstDecoder) -> presenter -> egl -> gbm
    //   -> restore -> card.
    // Each GstDecoder holds wrapped GLContext/GLDisplay refs
    // into blendr's EGL ctx; if egl drops first, gst-gl's
    // finalize segfaults dereferencing a dead EGLDisplay.
    // Streams::Drop tears down each pipeline (NULL) + joins
    // each pull thread before the inner GstDecoders deallocate.
    drop(streams);
    drop(pres);
    drop(egl);
    drop(gbm);

    // ALWAYS attempt restore, success or fail. Without this, the
    // screen stays black or the wrong FB stays scanned out.
    let restore_result = kms::restore(&card, &saved);
    if let Err(e) = &restore_result {
        log::error!("[blendr] restore failed: {e:#}");
    }

    drop(card);
    run_result.and(restore_result)
}

#[cfg(not(target_os = "linux"))]
fn run(_args: Args) -> Result<()> {
    anyhow::bail!(
        "blendr only builds + runs on Linux (target the Pi via \
         fresh/blendr/build.sh)"
    )
}
