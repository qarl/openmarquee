//! openMarquee renderer — Phase 1 bring-up.
//!
//! Phase 1 milestone: a Rust binary that opens DRM/KMS on the Pi,
//! enumerates HDMI connectors, and exits. This is the smallest
//! end-to-end test of the toolchain + crate stack on the canonical
//! hardware. Subsequent commits add GBM, EGL, GLES2, atomic page-flip
//! to reach pixels-on-HDMI.
//!
//! Run mode (placeholder, pre-pixels):
//!   openmarquee-render --output hdmi --probe
//!
//! Lists connectors / encoders / CRTCs / planes from /dev/dri/card{0,1}
//! and exits 0 on success. The eventual command shape is the standalone
//! mode from the plan: --playlist + --content-root + --settings + --output hdmi.

mod hdmi;

use std::fs::File;
use std::os::fd::{AsFd, BorrowedFd};
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use clap::{Parser, ValueEnum};
use drm::control::Device as ControlDevice;
use drm::Device;

/// Wrapper around a raw fd that satisfies `drm::Device` + `drm::control::Device`.
///
/// drm-rs trait implementations key off `AsFd`, so this thin newtype owning
/// a `File` is enough to talk to the kernel.
pub struct Card(pub File);

impl AsFd for Card {
    fn as_fd(&self) -> BorrowedFd<'_> {
        self.0.as_fd()
    }
}

impl Device for Card {}
impl ControlDevice for Card {}

impl Card {
    fn open(path: &Path) -> Result<Self> {
        let f = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)
            .with_context(|| format!("open {}", path.display()))?;
        Ok(Card(f))
    }
}

#[derive(Copy, Clone, Debug, ValueEnum)]
enum OutputMode {
    Hdmi,
    Mock,
}

#[derive(Parser, Debug)]
#[command(name = "openmarquee-render", version, about = "openMarquee renderer (Rust)")]
struct Args {
    /// Output target. Phase 1 only implements `hdmi --probe`; the rest is
    /// scaffolded for the standalone mode that comes after pixels-on-screen.
    #[arg(long, value_enum, default_value_t = OutputMode::Hdmi)]
    output: OutputMode,

    /// DRM card path. Defaults to scanning /dev/dri/card1 then /dev/dri/card0.
    #[arg(long)]
    drm_card: Option<PathBuf>,

    /// Probe-only mode (Phase 1). Open DRM, enumerate connectors/encoders/
    /// CRTCs/planes, print, exit 0. No GBM/EGL/GLES yet.
    #[arg(long, default_value_t = false)]
    probe: bool,

    /// Phase 2 — render a solid color via GBM + EGL + GLES2 + legacy
    /// SetCrtc, hold for `--hold-secs` seconds. Format: "R,G,B" (or
    /// "R,G,B,A") with each component in [0.0, 1.0]. Example:
    /// `--solid-color 0,0.5,1` for cyan-ish.
    #[arg(long, value_parser = parse_color)]
    solid_color: Option<[f32; 4]>,

    /// How long to hold the rendered frame on screen before exiting.
    /// Only used with `--solid-color`.
    #[arg(long, default_value_t = 5)]
    hold_secs: u64,

    /// Path to the playlist JSON for standalone mode (placeholder; not used
    /// in Phase 1).
    #[arg(long)]
    playlist: Option<PathBuf>,

    /// Path to the content root for standalone mode (placeholder; not used
    /// in Phase 1).
    #[arg(long)]
    content_root: Option<PathBuf>,

    /// Path to settings.json (placeholder; not used in Phase 1).
    #[arg(long)]
    settings: Option<PathBuf>,
}

fn open_drm(explicit: Option<&Path>) -> Result<(PathBuf, Card)> {
    if let Some(p) = explicit {
        return Ok((p.to_path_buf(), Card::open(p)?));
    }
    // vc4 KMS is typically card1 on Raspberry Pi OS Bookworm; card0 is sometimes
    // the v3d render-only node. Try card1 first.
    for cand in ["/dev/dri/card1", "/dev/dri/card0"] {
        let p = Path::new(cand);
        if p.exists() {
            match Card::open(p) {
                Ok(c) => return Ok((p.to_path_buf(), c)),
                Err(e) => {
                    eprintln!("warn: failed to open card {cand}: {e}; trying next");
                }
            }
        }
    }
    bail!("no usable DRM card found at /dev/dri/card{{0,1}}");
}

fn probe(card: &Card) -> Result<()> {
    let resources = card
        .resource_handles()
        .context("drmModeGetResources failed")?;

    println!("=== Connectors ===");
    for &handle in resources.connectors() {
        let info = match card.get_connector(handle, false) {
            Ok(info) => info,
            Err(e) => {
                println!("  {:?}: error: {}", handle, e);
                continue;
            }
        };
        println!(
            "  {:?}: {:?}-{} state={:?} modes={}",
            handle,
            info.interface(),
            info.interface_id(),
            info.state(),
            info.modes().len(),
        );
        for (i, m) in info.modes().iter().enumerate() {
            println!(
                "    mode[{}]: {}x{}@{} flags=0x{:x}",
                i,
                m.size().0,
                m.size().1,
                m.vrefresh(),
                m.mode_type().bits(),
            );
        }
    }

    println!("=== Encoders ===");
    for &handle in resources.encoders() {
        match card.get_encoder(handle) {
            Ok(info) => println!(
                "  {:?}: kind={:?} crtc={:?} possible_crtcs={:?}",
                handle,
                info.kind(),
                info.crtc(),
                info.possible_crtcs(),
            ),
            Err(e) => println!("  {:?}: error: {}", handle, e),
        }
    }

    println!("=== CRTCs ===");
    for &handle in resources.crtcs() {
        match card.get_crtc(handle) {
            Ok(info) => println!(
                "  {:?}: position={:?} mode_present={}",
                handle,
                info.position(),
                info.mode().is_some(),
            ),
            Err(e) => println!("  {:?}: error: {}", handle, e),
        }
    }

    println!("=== Planes ===");
    let plane_handles = card
        .plane_handles()
        .context("drmModeGetPlaneResources failed")?;
    for &handle in plane_handles.iter() {
        match card.get_plane(handle) {
            Ok(info) => println!(
                "  {:?}: crtc={:?} fb={:?} possible_crtcs={:?}",
                handle,
                info.crtc(),
                info.framebuffer(),
                info.possible_crtcs(),
            ),
            Err(e) => println!("  {:?}: error: {}", handle, e),
        }
    }

    Ok(())
}

fn parse_color(s: &str) -> Result<[f32; 4], String> {
    let parts: Vec<&str> = s.split(',').collect();
    if parts.len() != 3 && parts.len() != 4 {
        return Err(format!(
            "expected 3 or 4 comma-separated floats, got {}: {s:?}",
            parts.len()
        ));
    }
    let mut color = [0.0f32, 0.0, 0.0, 1.0];
    for (i, p) in parts.iter().enumerate() {
        color[i] = p
            .trim()
            .parse::<f32>()
            .map_err(|e| format!("component {i} ({p:?}): {e}"))?;
        if !(0.0..=1.0).contains(&color[i]) {
            return Err(format!("component {i} = {} out of [0,1]", color[i]));
        }
    }
    Ok(color)
}

fn main() -> Result<()> {
    let args = Args::parse();

    match args.output {
        OutputMode::Hdmi => {
            let (path, card) = open_drm(args.drm_card.as_deref())?;
            eprintln!("opened DRM device: {}", path.display());
            if args.probe {
                probe(&card)?;
                return Ok(());
            }
            if let Some(color) = args.solid_color {
                hdmi::render_solid_color(&card, color, args.hold_secs)?;
                return Ok(());
            }
            eprintln!("nothing to do — pass --probe or --solid-color R,G,B");
        }
        OutputMode::Mock => {
            eprintln!("mock output mode (placeholder); not yet implemented");
        }
    }

    Ok(())
}
