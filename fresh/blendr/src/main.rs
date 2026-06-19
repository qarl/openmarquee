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
mod kms;
mod signals;

use gles_present::Step;

#[derive(Parser, Debug)]
#[command(name = "blendr", version, about = "Phase 0 KMS-present milestone")]
struct Args {
    /// How long to render before tearing down and restoring scanout.
    #[arg(long, default_value_t = 30)]
    duration_sec: u64,

    /// Which Phase-0 step to render.
    ///   solid   = hue-cycling clear color (proves swap+flip).
    ///   checker = static 256x256 checkerboard via GLES2 shader
    ///             (proves the GLES2 path is up too).
    #[arg(long, value_enum, default_value_t = Step::Checker)]
    step: Step,

    /// Bypass /dev/dri/card* auto-probe. Use only for debug.
    #[arg(long)]
    card_override: Option<PathBuf>,
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

    let run_result = kms::run_loop(
        &card,
        &mode_pick,
        &mut gbm,
        &mut egl,
        &mut pres,
        args.duration_sec,
        &signals::EXIT_REQUESTED,
    );

    // Drop GLES presenter + EGL + GBM in reverse-of-init order so
    // the GL context is current when its textures are deleted, and
    // the GBM surface is alive when EGL destroys its EGLSurface.
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
