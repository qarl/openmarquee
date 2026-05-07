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

// Hardware bring-up modules link against drm/gbm/EGL — Linux-only at
// build + link time. Gating them keeps `cargo test` runnable on the
// Mac dev box for the pure-logic surfaces.
#[cfg(target_os = "linux")]
mod hdmi;
mod content;
mod hdmi_logic;

use std::path::PathBuf;

use anyhow::{bail, Result};
use clap::{Parser, ValueEnum};

#[cfg(target_os = "linux")]
use std::fs::File;
#[cfg(target_os = "linux")]
use std::os::fd::{AsFd, BorrowedFd};
#[cfg(target_os = "linux")]
use std::path::Path;
#[cfg(target_os = "linux")]
use anyhow::Context;
#[cfg(target_os = "linux")]
use drm::control::Device as ControlDevice;
#[cfg(target_os = "linux")]
use drm::Device;

/// Wrapper around a raw fd that satisfies `drm::Device` + `drm::control::Device`.
///
/// drm-rs trait implementations key off `AsFd`, so this thin newtype owning
/// a `File` is enough to talk to the kernel. Linux-only — the trait impls
/// require drm-rs which doesn't link on macOS.
#[cfg(target_os = "linux")]
pub struct Card(pub File);

#[cfg(target_os = "linux")]
impl AsFd for Card {
    fn as_fd(&self) -> BorrowedFd<'_> {
        self.0.as_fd()
    }
}

#[cfg(target_os = "linux")]
impl Device for Card {}
#[cfg(target_os = "linux")]
impl ControlDevice for Card {}

#[cfg(target_os = "linux")]
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

    /// Phase 2.1 — animate a full-screen HSV hue rotation via DRM
    /// atomic commit + double-buffered scanout. Proves the per-frame
    /// rendering loop, atomic property writes, page-flip events, and
    /// buffer rotation all work cleanly. This is plan §4 Step 2 — the
    /// foundation every subsequent phase (slide bake, transitions,
    /// video) builds on. Holds for `--hold-secs` seconds.
    #[arg(long, default_value_t = false)]
    animate: bool,

    /// Phase 4 entry — load a TextSlide by UUID from the configured
    /// playlist + content_root and render its background_color.
    /// Smallest end-to-end test of the playlist→content→GLES path.
    /// Procedural background_pattern (12 patterns) lands in a
    /// follow-up commit.
    #[arg(long)]
    play_slide: Option<uuid::Uuid>,

    /// Phase 4.2a — load a TextSlide by UUID and render its FIRST
    /// visible text_layer composited over the bg color. Smallest
    /// end-to-end test of the layout→atlas-upload→shader→composite
    /// path. Multi-layer + gradient-bg-with-text composite land in
    /// 4.2b/4.2c. Uses the font at `--font-path`.
    #[arg(long)]
    play_slide_text: Option<uuid::Uuid>,

    /// Phase 5-a — render a slide via the offscreen-FBO path
    /// (paint into a color texture, then blit to screen via a
    /// textured quad). Visual output is identical to --play-slide;
    /// the flag exists so smoke can verify the FBO path is wired
    /// correctly before Phase 5-b's transitions land.
    #[arg(long)]
    play_slide_via_fbo: Option<uuid::Uuid>,

    /// Directory holding the renderer's font catalog. Each layer's
    /// `font_family` is mapped to a TTF basename under this dir
    /// (Anton → anton.ttf, "Bebas Neue" → bebas-neue.ttf, etc.).
    /// Defaults to the Pi deploy path.
    #[arg(long, default_value = "/opt/openmarquee/ui/fonts")]
    font_dir: PathBuf,

    /// Fallback font family used when a layer's `font_family` isn't
    /// recognized OR its TTF can't be loaded. Defaults to "Anton"
    /// (the FYS canonical face). If even the fallback can't be
    /// loaded, layers are skipped with a warning rather than the
    /// whole slide failing.
    #[arg(long, default_value = "Anton")]
    fallback_font_family: String,

    /// Target frame rate for `--animate`. The atomic-commit page-flip
    /// loop caps to display vrefresh regardless; this just sets the
    /// animation speed (hue cycle period).
    #[arg(long, default_value_t = 30)]
    fps: u32,

    /// How long to hold the rendered frame on screen before exiting.
    /// Used by `--solid-color` and `--animate`.
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

#[cfg(target_os = "linux")]
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

#[cfg(target_os = "linux")]
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

#[cfg(test)]
mod tests {
    use super::parse_color;

    #[test]
    fn parse_color_three_components() {
        assert_eq!(parse_color("0,0.5,1"), Ok([0.0, 0.5, 1.0, 1.0]));
    }

    #[test]
    fn parse_color_four_components_explicit_alpha() {
        assert_eq!(parse_color("1,0,0,0.5"), Ok([1.0, 0.0, 0.0, 0.5]));
    }

    #[test]
    fn parse_color_default_alpha_is_one() {
        let c = parse_color("0.2,0.4,0.6").unwrap();
        assert_eq!(c[3], 1.0);
    }

    #[test]
    fn parse_color_trims_whitespace_per_component() {
        assert_eq!(parse_color(" 0.5 , 0.5 , 0.5 "), Ok([0.5, 0.5, 0.5, 1.0]));
    }

    #[test]
    fn parse_color_rejects_two_components() {
        let err = parse_color("0.5,0.5").unwrap_err();
        assert!(err.contains("got 2"), "msg: {err}");
    }

    #[test]
    fn parse_color_rejects_five_components() {
        let err = parse_color("0,0,0,0,0").unwrap_err();
        assert!(err.contains("got 5"), "msg: {err}");
    }

    #[test]
    fn parse_color_rejects_above_unit_range() {
        let err = parse_color("0,0,1.5").unwrap_err();
        assert!(err.contains("out of [0,1]"), "msg: {err}");
    }

    #[test]
    fn parse_color_rejects_below_unit_range() {
        let err = parse_color("0,-0.1,0").unwrap_err();
        assert!(err.contains("out of [0,1]"), "msg: {err}");
    }

    #[test]
    fn parse_color_rejects_non_numeric() {
        let err = parse_color("0,red,0").unwrap_err();
        assert!(err.contains("component 1"), "msg: {err}");
    }

    #[test]
    fn parse_color_zero_zero_zero_is_valid_black() {
        assert_eq!(parse_color("0,0,0"), Ok([0.0, 0.0, 0.0, 1.0]));
    }

    #[test]
    fn parse_color_one_one_one_is_valid_white() {
        assert_eq!(parse_color("1,1,1"), Ok([1.0, 1.0, 1.0, 1.0]));
    }
}

fn main() -> Result<()> {
    let args = Args::parse();

    match args.output {
        OutputMode::Hdmi => {
            #[cfg(target_os = "linux")]
            {
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
                if args.animate {
                    hdmi::render_animated_atomic(&card, args.hold_secs, args.fps)?;
                    return Ok(());
                }
                // Phase 4.2b: --play-slide and --play-slide-text now
                // route through the same unified render_slide path
                // (bg + first text layer in one frame). The two flags
                // differ only in playlist-sanity-check behavior:
                // --play-slide loads the playlist as a wiring sanity
                // check; --play-slide-text bypasses it for tighter
                // smoke-test isolation.
                //
                // Phase 5-a adds --play-slide-via-fbo on the same
                // closure. `via_fbo` selects which renderer fn the
                // closure calls.
                let dispatch_slide = |slide_id: uuid::Uuid,
                                      load_playlist: bool,
                                      via_fbo: bool|
                 -> Result<()> {
                    let content_root = args
                        .content_root
                        .as_deref()
                        .unwrap_or_else(|| Path::new("/var/openmarquee/content"));
                    if load_playlist {
                        let playlist_path = args
                            .playlist
                            .as_deref()
                            .unwrap_or_else(|| Path::new("/var/openmarquee/playlist.json"));
                        let _env = content::load_playlist(playlist_path)?;
                    }
                    let slide = content::find_text_slide(content_root, slide_id)?
                        .ok_or_else(|| anyhow::anyhow!(
                            "no text_slide found for {slide_id} under {}",
                            content_root.display(),
                        ))?;
                    // Phase 4.2c-4: per-layer font lookup via the
                    // catalog. If the catalog can't even load the
                    // fallback (font_dir missing/empty), pass None
                    // so the slide renders bg-only rather than
                    // failing the whole call — operator gets a
                    // clear log line.
                    let catalog = hdmi_logic::FontCatalog::new(
                        args.font_dir.clone(),
                        args.fallback_font_family.clone(),
                    );
                    let catalog_opt = if catalog.fallback_available() {
                        Some(&catalog)
                    } else {
                        eprintln!(
                            "warn: font catalog at {} can't load fallback {:?} \
                             — rendering bg only",
                            args.font_dir.display(),
                            args.fallback_font_family,
                        );
                        None
                    };
                    if via_fbo {
                        hdmi::render_slide_via_fbo(&card, &slide, catalog_opt, args.hold_secs)
                    } else {
                        hdmi::render_slide(&card, &slide, catalog_opt, args.hold_secs)
                    }
                };

                if let Some(slide_id) = args.play_slide {
                    dispatch_slide(slide_id, true, false)?;
                    return Ok(());
                }
                if let Some(slide_id) = args.play_slide_text {
                    dispatch_slide(slide_id, false, false)?;
                    return Ok(());
                }
                if let Some(slide_id) = args.play_slide_via_fbo {
                    dispatch_slide(slide_id, false, true)?;
                    return Ok(());
                }
                eprintln!("nothing to do — pass --probe, --solid-color R,G,B, --animate, --play-slide UUID, --play-slide-text UUID, or --play-slide-via-fbo UUID");
            }
            #[cfg(not(target_os = "linux"))]
            {
                let _ = &args;
                bail!("--output hdmi requires Linux (drm/gbm/EGL); not available on this host");
            }
        }
        OutputMode::Mock => {
            eprintln!("mock output mode (placeholder); not yet implemented");
        }
    }

    Ok(())
}
