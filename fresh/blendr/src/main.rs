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

    /// Required when --step blend. First clip (stream A).
    #[arg(long)]
    clip_a: Option<PathBuf>,

    /// Required when --step blend. Second clip (stream B).
    #[arg(long)]
    clip_b: Option<PathBuf>,

    /// Phase 2 blend alpha: 0.0 = pure A, 1.0 = pure B, 0.5 =
    /// 50/50 dissolve (default). Phase 3 will animate this; for
    /// now it stays static across the run.
    #[arg(long, default_value_t = 0.5)]
    alpha: f32,

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
            let clip_a = args.clip_a.as_deref().ok_or_else(|| {
                anyhow::anyhow!("--step blend requires --clip-a <PATH>")
            })?;
            let clip_b = args.clip_b.as_deref().ok_or_else(|| {
                anyhow::anyhow!("--step blend requires --clip-b <PATH>")
            })?;
            let a = gst_decode::GstDecoder::new(&egl, clip_a)
                .context("GstDecoder::new (blend A)")?;
            let b = gst_decode::GstDecoder::new(&egl, clip_b)
                .context("GstDecoder::new (blend B)")?;
            kms::Streams::Blend { a, b, alpha: args.alpha }
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
