//! openMarquee renderer — LIBRARY entry point.
//!
//! 2026-06-17 QA M2 dispatch — pivot to Option A (lib.rs restructure)
//! after two rounds of M2 hand-wired display path failing identically
//! (single video painting black in PREOPEN phase = wiring bug, not the
//! r76 freeze we're hunting). QA call: stop reinventing the display
//! path; expose the real one as a library so the M2 probe can call
//! the ACTUAL `run_in_egl_session` + `bake_video_slide_to_current_fbo`
//! instead of a lifted copy.
//!
//! ## Why this file exists separately from `main.rs`
//!
//! The renderer is shipped as a `[[bin]]` binary (`openmarquee-render`).
//! Its module tree is declared at the top of `main.rs`. The M0/M1/M2
//! probes are also `[[bin]]` targets in `src/bin/`; the M0/M1 probes
//! used `#[path = "../v4l2.rs"] mod v4l2;`-style path-imports to reach
//! the renderer's internal modules (each probe got its own compiled
//! copy of the small modules — `v4l2`, `mp4_demux`, `video_decode`,
//! `probe_oracle`, `frame_pacing`).
//!
//! That pattern DOES NOT SCALE to `hdmi.rs`: it transitively pulls in
//! ~10 crate-level modules (content / hdmi_logic / glyph_cache /
//! sdf_atlas / sdf_atlas_gl / atlas_page / glyph_cache_colr / ...).
//! Path-importing the full closure is tedious + brittle.
//!
//! M2 also requires the REAL display path (per QA's import-not-
//! reinvent guard fired twice on the hand-wired copy). So this lib
//! exposes the renderer's module tree as `openmarquee_render::*`
//! that any `[[bin]]` in this package can `use` directly.
//!
//! ## Faithfulness
//!
//! The `[[bin]] openmarquee-render` (= main.rs) keeps its own module
//! declarations + builds + behaves IDENTICALLY to before this lib
//! existed. Cargo lowers the lib + bin as separate compilation
//! units that happen to share source files; the production binary's
//! emitted code is byte-equivalent to the pre-restructure build
//! (verified at the cross-build gate before any M2 work proceeds).
//!
//! ## Future
//!
//! When the eventual clean renderer ships (Milestone 1 of
//! /tmp/handoffs/little-renderer-transitions-20260617/SPEC.md), it
//! reuses this lib structure — `openmarquee_render::hdmi::*` becomes
//! the parts library QA promised in SPEC §"SCOPE — what to LIFT vs
//! WRITE FRESH". M2 is the proof-of-concept for this lib path.
//!
//! See main.rs for the binary entry point + CLI handling. Items
//! declared `pub` here mirror main.rs's `mod X;` + crate-root
//! declarations.

// =====================================================================
// Module tree — same cfg gates and declaration order as main.rs's
// `mod X;` block.
// =====================================================================

#[cfg(target_os = "linux")]
pub mod hdmi;
#[cfg(target_os = "linux")]
pub mod image_slide_tex;
pub mod live_preview;
pub mod cea861;
pub mod content;
pub mod hdmi_logic;
pub mod ipc_main;
pub mod lru;
pub mod mem;
pub mod mp4_demux;
pub mod playback;
pub mod profile;
pub mod sigusr1;
pub mod frame_pacing;
pub mod sdf_atlas;
#[cfg(target_os = "linux")]
pub mod sdf_atlas_gl;
pub mod sdf_atlas_emoji;
#[cfg(target_os = "linux")]
pub mod gl_subtexture_smoke;
pub mod atlas_page;
pub mod glyph_cache;
pub mod glyph_cache_colr;
pub mod v4l2;
#[cfg(target_os = "linux")]
pub mod video_decode;
// M0/M1/M2 probe pixel oracle. Linux-only because the consumers are
// the V4L2-backed probes; the module itself is pure-Rust but is
// gated to match its dependents.
#[cfg(target_os = "linux")]
pub mod probe_oracle;

// =====================================================================
// Crate-root pub items referenced by modules via `crate::X` paths.
// Mirrored from main.rs — VALUES MUST STAY IN SYNC.
// =====================================================================

/// Cold-start (immediate-playback) warmup count. See main.rs's
/// docstring for the full r73/r77/r93/r94/Phase-B history. The
/// value here must match main.rs's; both compilations need the
/// same const or `prime_video_decoder_with_warmup` behaves
/// differently between the bin and lib paths.
pub const PRIME_WARMUP_DEFAULT: usize = 2;
/// Preload-path warmup count. See main.rs for the load-bearing
/// docstring.
pub const PRIME_WARMUP_FOR_PRELOAD: usize = 2;
/// Cold-start CAPTURE pool floor. See main.rs.
pub const PRIME_K_FLOOR_DEFAULT: usize = PRIME_WARMUP_DEFAULT + 1;
/// Preload-path CAPTURE pool floor. See main.rs for the r77 hard
/// guarantee preserved here.
pub const PRIME_K_FLOOR_FOR_PRELOAD: usize = PRIME_WARMUP_FOR_PRELOAD + 2;

// =====================================================================
// Card — DRM device wrapper. Same shape + impls as main.rs:310.
// =====================================================================

#[cfg(target_os = "linux")]
use std::fs::File;
#[cfg(target_os = "linux")]
use std::os::fd::{AsFd, BorrowedFd};

/// Thin newtype around a `/dev/dri/card{0,1}` file. drm-rs trait
/// implementations key off `AsFd`, so this struct + its AsFd
/// impl + the empty Device + ControlDevice trait impls are enough
/// for the renderer's KMS/DRM calls.
#[cfg(target_os = "linux")]
pub struct Card(pub File);

#[cfg(target_os = "linux")]
impl AsFd for Card {
    fn as_fd(&self) -> BorrowedFd<'_> { self.0.as_fd() }
}

#[cfg(target_os = "linux")]
impl drm::Device for Card {}
#[cfg(target_os = "linux")]
impl drm::control::Device for Card {}

#[cfg(target_os = "linux")]
impl Card {
    /// Open the DRM device at `path` (typically /dev/dri/card0 or
    /// card1). Bubbles up the open error with context per main.rs.
    pub fn open(path: &std::path::Path) -> anyhow::Result<Self> {
        use anyhow::Context;
        let f = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)
            .with_context(|| format!("open {}", path.display()))?;
        Ok(Card(f))
    }
}

// =====================================================================
// AaMode — referenced by hdmi_logic.rs's MSDF shader dispatch.
// =====================================================================

/// SDF arc slice B — AA mode for MSDF text rendering. Same enum as
/// main.rs:351. `clap::ValueEnum` derived in main.rs is binary-only
/// (the probes don't parse CLI), so this lib version omits the
/// `ValueEnum` derive — the discriminants + variants match.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum AaMode {
    Fwidth,
    Fixed,
}

impl AaMode {
    /// Human-readable form for the slice-D parity HUD line.
    pub fn as_str(self) -> &'static str {
        match self {
            AaMode::Fwidth => "fwidth",
            AaMode::Fixed => "fixed",
        }
    }
}

// =====================================================================
// ForcedMode — referenced by hdmi.rs's synthesize_drm_mode.
// =====================================================================

/// v1-spec-delta #2 (slice d-smoke) — `--force-mode WxH@Hz` CLI
/// override. Same shape as main.rs:760. `parse()` omitted from the
/// lib (CLI parsing is binary-only); hdmi.rs reads the fields
/// directly via the `pub` modifiers.
#[derive(Copy, Clone, Debug)]
pub struct ForcedMode {
    pub width: u16,
    pub height: u16,
    pub vrefresh_hz: u32,
}
