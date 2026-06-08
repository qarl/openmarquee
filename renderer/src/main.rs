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
/// Task #168 (2026-05-22) — async-refresh GL texture cache for
/// ImageSlide / WebSlide. Linux-only (depends on `glow`); see module
/// docs for the state-machine. Replaces the per-paint PNG decode +
/// glTexImage2D + delete cycle that hitched the render thread by
/// 100-300ms on every Web-slide refresh transition.
#[cfg(target_os = "linux")]
mod image_slide_tex;
mod cea861;
mod content;
mod hdmi_logic;
mod ipc_main;
mod lru;
mod mem;
mod mp4_demux;
mod playback;
mod profile;
/// r38d (2026-06-02) — SIGUSR1 cache-dump handler. Async-signal-safe
/// AtomicBool flag + dump-emitter. Used by ipc_main.rs's inner loop
/// to drop a [cache-dump] line on stderr (journald-visible) when an
/// operator sends `pkill -USR1 -f openmarquee-render`. See
/// qa/r38d-sigusr1-cache-dump-2026-06-02.md.
mod sigusr1;
/// `[perf]` r1 (2026-05-26) — host-testable predicate for the
/// 30fps deadline-miss counter wired into `commit_fb`. Pure data;
/// cross-platform so the boundary tests (0/35/36/37/100ms) run on
/// Mac as well as the Pi. The production constants + warn-log live
/// in `hdmi.rs`; this module is just the predicate + tests.
mod frame_pacing;
/// SDF arc slice B -- compile-time-baked MSDF atlas pipeline.
/// Cross-platform parsing + per-glyph lookup.
mod sdf_atlas;
/// SDF arc slice B.2 -- GL-side of the MSDF atlas pipeline. Linux-
/// only (depends on `glow`); uploads parsed atlases as GL_RGB
/// textures at EGL bring-up.
#[cfg(target_os = "linux")]
mod sdf_atlas_gl;
/// Slice 3D (2026-05-19) — post-CBDT-retire shim. Module now
/// exports only `codepoint_is_emoji_range`; the CBDT bake +
/// EmojiAtlas + EmojiAtlasEntry + load_emoji_atlas are gone.
/// Emoji codepoints route to `glyph_cache_colr` via the Slice
/// 3B dispatch hook in `hdmi_logic.rs`.
mod sdf_atlas_emoji;
/// Bug 3 Slice 1 prep (2026-05-19) -- standalone glTexSubImage2D
/// smoke test. Verifies vc4 driver behavior before committing the
/// runtime glyph cache infrastructure that depends on it. Module
/// will be deleted once Slice 1 lands + is exercised by real loads.
#[cfg(target_os = "linux")]
mod gl_subtexture_smoke;
/// Bug 3 Slice 1 (2026-05-19) -- runtime glyph cache infrastructure.
/// HYBRID architecture per qa-Jimmy option D: static MSDF atlas
/// (sdf_atlas) covers build-time-baked codepoints; this dynamic
/// cache covers everything else. Slice 1 lands the infrastructure
/// + stub worker; Slice 2 wires the msdfgen-based real worker that
/// rasterizes ●/∞/etc. on cache-miss.
///
/// Modules are cross-platform-compiled (CPU-side state machine + slot
/// allocation tests run on Mac via cargo test); GL-touching fns
/// (allocate_texture / upload_slot / poll_completions) are
/// individually cfg-gated to target_os="linux" so Mac builds don't
/// need glow at link time.
mod atlas_page;
mod glyph_cache;
// Bug 3 Slice 3A (2026-05-19): runtime COLRv1 vector emoji raster.
// Skeleton + worker dispatch only — no caller emits RenderMode::Colr
// requests until Slice 3B wires the layout_text_to_quads hook.
mod glyph_cache_colr;
// V4L2 piece 2a (2026-05-14): scaffold for the bcm2835-codec H.264
// M2M decoder client. Open + capability query are wired; format-set,
// REQBUFS, STREAMON, and the decode loop are stubbed for piece 2b.
// Module is declared unconditionally so Mac `cargo check` syntax-
// validates the file; the items that link against libc/nix are
// individually `#[cfg(target_os = "linux")]` gated inside the
// module so Mac compiles + runs the pure-Rust tests.
mod v4l2;
/// QA H2 (2026-05-23) — V4L2 H.264 decoder priming + per-slide
/// state, shared between the IPC sidecar and the standalone reel.
/// Linux-only; the module's `#![cfg(target_os = "linux")]` collapses
/// it to a no-op on Mac.
#[cfg(target_os = "linux")]
mod video_decode;

// r73 (2026-06-06): V4L2 prime warmup-count tunables. Live at
// crate root (not inside the Linux-gated video_decode module) so
// cross-platform tests can pin them on the macOS dev host.
//
// Each prime_video_decoder*() call QBUFs `1 primer + N warmup`
// samples into the 4-slot OUTPUT pool. The kernel decodes them
// into the 4-slot CAPTURE pool; with CAPTURE saturated,
// bcm2835-codec can't release pinned OUTPUT buffers back to
// userspace. See PRIME_WARMUP_FOR_PRELOAD for the r73 root-cause
// chain.

/// Cold-start (immediate-playback) warmup count. r57 chose 3 so
/// the total feed sequence (1 primer + 3 warmup = 4) fully fills
/// the 4-slot OUTPUT pool, maximizing the kernel's parallel
/// decode depth for the bake loop that consumes one CAPTURE
/// frame per tick within ~33ms.
pub const PRIME_WARMUP_DEFAULT: usize = 3;

/// Preload-path warmup count. Used by the r65 async PreloadSlide
/// worker, whose decoder sits idle for ~1s between prime exit and
/// the transition's first bake call.
///
/// r73 (2026-06-06) initially set this to 1 to avoid CAPTURE-pool
/// saturation: at warmup_count=3 the prime QBUFs 4 OUTPUT samples
/// that the kernel decodes into 4 CAPTURE buffers (CAPTURE pool is
/// also 4). With CAPTURE fully queued, bcm2835-codec couldn't
/// release the pinned OUTPUT buffers back to userspace, so when
/// the transition started ~1s later, drain_output_quiet returned
/// EAGAIN on every retry (r70 perf data: waited_us=102000
/// retries=25, pool_empty_on_entry=true).
///
/// r77 (2026-06-07) bumps this to 2 after empirical refutation of
/// the r73 invariant. Phase B telemetry from FYS:
///   preloads=11
///   preload_handoff frames_drained=0  (ALL 11)
///   preload_handoff drain_us=503000   (default 500ms budget timed out)
/// 4x'ing the drain budget to 2000ms produced ZERO additional
/// frames. The decoder was input-starved: 1 primer + 1 warmup =
/// 2 samples isn't enough for bcm2835-codec to produce a decoded
/// CAPTURE frame. Most-likely cause is that sample 1 is a B-frame
/// depending on a future P-frame that wasn't fed -- typical H.264
/// content but content-dependent (open-GOP / baseline-profile
/// streams behave differently).
///
/// =2 means prime feeds 3 samples (primer + 2 warmup). If the
/// reorder distance is 3-4 (per the existing video_decode.rs
/// comment), 3 samples should usually be enough to deliver the
/// IDR's decoded CAPTURE frame.
///
/// r73's "saturation wedges OUTPUT release" failure mode now has
/// a different fix in place: r76 Phase B's post-prime CAPTURE
/// drain consumes one Frame whose Drop re-QBUFs the buffer back
/// to the kernel pool. At warmup=2 specifically, the math
/// independently keeps us safe: 3 OUTPUT samples queued -> kernel
/// can produce at most 3 CAPTURE frames -> pool of 4 has at
/// least 1 free slot whether or not the drain succeeds.
///
/// r93 (2026-06-08) BUMPED TO 3 after the EOS-flush arc
/// (r80/r81/r82/r85/r87/r91/r92) closed empty. qarl push-back:
/// "video transitions used to work." The architectural regression
/// was the r65 async preload + r73 warmup_count split itself:
/// cold-start at warmup_count=3 produces frames reliably; preload
/// at warmup=1 (or =2 per r77) never produces frame 0. The whole
/// CMD_STOP arc was trying to FORCE the kernel to emit a frame the
/// driver wasn't ready to commit -- r92 proved CMD_STOP itself is
/// poisonous on bcm2835-codec. Simplest possible fix per QA r93
/// dispatch: feed more samples (3) at preload, matching cold-start.
///
/// Saturation concern (the original r73 invariant):
///   At warmup=3, prime QBUFs 4 OUTPUT samples; kernel decodes
///   into 4 CAPTURE buffers; CAPTURE pool is 4 -> saturates;
///   bcm2835-codec can't release pinned OUTPUT.
///
/// Why r93 ships it anyway (per QA dispatch):
///   1. Cold-start uses warmup=3 in production and works. Proof
///      saturation isn't always fatal.
///   2. Existing instrumentation (vpu_mmal_components, REQBUFS
///      EINVAL, dmesg leaving_buffer) will pin saturation
///      immediately if it bites. Roll back to =2 + ship Path B
///      (drain at consumer not producer) if it does.
///
/// History:
/// - r73 (2026-06-06) initially set this to 1 to avoid saturation.
/// - r77 (2026-06-07) bumped to 2 after Phase B telemetry refuted
///   r73's invariant: warmup=1 never produced a decoded frame.
/// - r93 (2026-06-08) bumped to 3 after the EOS-flush arc proved
///   CMD_STOP isn't a viable signal on bcm2835-codec. Feed more,
///   don't signal.
pub const PRIME_WARMUP_FOR_PRELOAD: usize = 3;

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

/// SDF arc slice B -- AA mode for MSDF text rendering.
///
/// `Fwidth` uses GLES2's `GL_OES_standard_derivatives` `fwidth()`
/// builtin to compute screen-space AA width per fragment -- correct
/// across arbitrary on-screen scale, no per-call uniforms. The vc4
/// driver advertises the extension but Mesa documents a "wrong
/// precision" warning; whether that's noise or unusable for SDF AA
/// is the open empirical question (recon §3, QA dispatch accepted
/// "test by deploy-time parity" path).
///
/// `Fixed` skips derivatives, uses a per-draw uniform AA width set
/// from the on-screen quad height. Cheaper, more deterministic, but
/// the AA width doesn't track per-fragment scale (won't compensate
/// for rotation / non-uniform scale; we don't currently use those).
#[derive(Copy, Clone, Debug, ValueEnum, PartialEq, Eq)]
pub enum AaMode {
    /// Adaptive: smoothstep half-width = fwidth(d) per fragment.
    Fwidth,
    /// Fixed-pixel: smoothstep half-width = uniform from CPU.
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

#[derive(Copy, Clone, Debug, ValueEnum)]
enum OutputMode {
    Hdmi,
    /// v1-spec-delta #11 (slice d) -- Mac dev preview path.
    /// Slice (d) ships a clean reach gate: --output mock
    /// emits a diagnostic identifying what's wired (snapshot
    /// capture is the only real Mac-side primitive today, via
    /// --capture-slide; per-frame paint requires GLES2 / EGL /
    /// DRM which Mac doesn't have). Future phases can extend
    /// Mock to a virtual-scanout PNG sequence.
    Mock,
    /// v1-spec-delta #11 (slice e) -- 64x64 HUB75 RGB matrix
    /// panel reach gate. Spec §5: keep reachable; panel-write
    /// path stubbed for v1.
    Hub75,
    /// v1-spec-delta #11 (slice e) -- WS2812B serial-RGB strip
    /// reach gate. Spec §5: keep reachable; panel-write path
    /// stubbed for v1.
    Ws2812b,
}

#[derive(Parser, Debug)]
#[command(name = "openmarquee-render", version, about = "openMarquee renderer (Rust)")]
struct Args {
    /// Output target. Phase 1 only implements `hdmi --probe`; the rest is
    /// scaffolded for the standalone mode that comes after pixels-on-screen.
    #[arg(long, value_enum, default_value_t = OutputMode::Hdmi)]
    output: OutputMode,

    /// SDF arc slice B -- which AA path the MSDF fragment shaders use.
    /// Default `fixed` is empirically required: vc4 (Pi Zero 2 W /
    /// VideoCore IV) does NOT support `GL_OES_standard_derivatives`
    /// so the fwidth() variant fails to compile with
    /// "error: no function with name 'fwidth'". The fixed-AA variant
    /// uses a uniform aa_width and works on every GLES2 driver.
    /// Slice B.2 Pi captures (2026-05-17) confirmed fwidth shader
    /// failure on vc4 + fixed variant pixel-correct rendering across
    /// 4 representative FYS slides.
    #[arg(long, value_enum, default_value_t = AaMode::Fixed)]
    aa_mode: AaMode,

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

    /// Phase 5-b-1 — render a single-frame fade composite of two
    /// slides at a fixed `--fade-t` ∈ [0, 1]. Smallest test of the
    /// dual-FBO + FS_FADE blend path. Phase 5-b-2 wraps this in a
    /// per-frame loop driving t over `transition_ms`. Format:
    /// `--fade-from UUID --fade-to UUID --fade-t 0.5`.
    #[arg(long)]
    fade_from: Option<uuid::Uuid>,

    /// Destination slide for `--fade-from`.
    #[arg(long)]
    fade_to: Option<uuid::Uuid>,

    /// Transition `t` for the fade composite (0.0 = source, 1.0 =
    /// destination, 0.5 = even cross-fade).
    #[arg(long, default_value_t = 0.5)]
    fade_t: f32,

    /// Phase 5-b-2 — animate the transition between `--fade-from`
    /// and `--fade-to` over `--transition-ms` at `--fps`. When this
    /// flag is set, `--fade-t` is ignored.
    #[arg(long, default_value_t = false)]
    animate_fade: bool,

    /// Phase 5-c — transition kind to run when `--animate-fade` is
    /// set. Currently supported: `cut` / `fade` / `wipe`. Unknown
    /// kinds fall back to `cut` with a warn. The Python content
    /// model has 16 kinds total; remaining 13 land in 5-c-2/3/etc.
    #[arg(long, default_value = "fade")]
    transition: String,

    /// Animated transition duration in milliseconds.
    #[arg(long, default_value_t = 800)]
    transition_ms: u32,

    /// Phase 6 — walk the playlist sequentially and render every
    /// text-slide item with its entry transition. Each item's
    /// `transition` / `transition_ms` from playlist.json drives
    /// the inter-slide animation; `slide.duration_ms` (or
    /// `--hold-secs` if set) controls the hold per slide.
    #[arg(long, default_value_t = false)]
    play_reel: bool,

    /// v1-spec-delta #11 (slice a) -- snapshot capture: render a
    /// TextSlide once into the EGL window surface, read back via
    /// glReadPixels, flip-Y, and encode to a PNG at the given
    /// path. No scanout commit -- this is an offscreen capture
    /// for spec §7.3 /capture/<id>.png endpoints (and the IPC
    /// sidecar Capture op).
    #[arg(long)]
    capture_slide: Option<uuid::Uuid>,

    /// v1-spec-delta #11 (slice a) -- output path for
    /// `--capture-slide`. Required when --capture-slide is set.
    #[arg(long)]
    capture_path: Option<PathBuf>,

    /// Visual-verdict capture: render one frame at t=0.5 (or
    /// --capture-sb-t) of a transition between --fade-from and
    /// --fade-to through the atlas SB pipeline, write PNG at
    /// --capture-path. Combine with --transition to pick the kind.
    ///
    /// Example:
    ///   /tmp/openmarquee-render --output hdmi \\
    ///     --capture-sb-mid \\
    ///     --fade-from <a> --fade-to <b> --transition cut \\
    ///     --capture-path /tmp/sb-mid.png
    #[arg(long, default_value_t = false)]
    capture_sb_mid: bool,

    /// QA-direct (2026-05-09) -- override the t value the
    /// capture-sb-mid samples at. Default 0.5 (mid-transition).
    /// Also used by --capture-fullres-mid.
    #[arg(long, default_value_t = 0.5)]
    capture_sb_t: f32,

    /// QA-direct (2026-05-13) -- override the motion `tick_seconds`
    /// value used by both --capture-sb-mid and --capture-fullres-mid
    /// when computing motion_states. Without this flag the captures
    /// use tick=0 (Batch 17.fix-A bless reproducibility pin); with
    /// this flag, motion is rendered at the specified tick so the
    /// SB-vs-fullres parity comparison can exercise motion-in-flight
    /// (closes the "motion frozen" limitation flagged in
    /// qa/atlas-sb-visual-sanity-2026-05-13.md). Per-fixture re-bless
    /// is required when the tick changes.
    #[arg(long)]
    capture_motion_tick: Option<f64>,

    /// QA-direct (2026-05-13) Atlas SB visual-sanity counterpart
    /// to --capture-sb-mid. Takes the SAME args (--fade-from /
    /// --fade-to / --transition / --capture-sb-t / --capture-path
    /// / --content-root) but bakes both slides at full mode
    /// resolution into per-slide FBOs instead of two half-res atlas
    /// regions. The PNG is the full-res reference baseline that
    /// the SB-path golden can be SSIM-diffed against. SSIM gate is
    /// 0.95 per spec §11 / Atlas SB acceptance.
    ///
    /// Example:
    ///   /tmp/openmarquee-render --output hdmi \\
    ///     --capture-fullres-mid \\
    ///     --fade-from <a> --fade-to <b> --transition fade \\
    ///     --capture-sb-t 0.5 \\
    ///     --capture-path /tmp/fullres-mid.png \\
    ///     --content-root /path/to/content
    #[arg(long, default_value_t = false)]
    capture_fullres_mid: bool,

    /// Batch 17.2 / sweep #9 #2 -- pin tick_seconds for
    /// --capture-slide so motion compositor evaluates at a fixed
    /// phase. Without this flag, capture renders the slide at
    /// tick=0 + real wall-clock-time, which produces a different
    /// PNG every run for any layer with motion=ticker/breathe/
    /// pulse/bounce/blink/shake. With this flag, the same tick
    /// reproduces bit-identical for golden-master diffs.
    ///
    /// Example:
    ///   /tmp/openmarquee-render --output hdmi \\
    ///     --capture-slide <UUID> --content-root <ROOT> \\
    ///     --capture-slide-at-tick 1.75 \\
    ///     --capture-path /tmp/animated.png
    #[arg(long)]
    capture_slide_at_tick: Option<f64>,

    /// v1-spec-delta #9 (slice c+) -- enter IPC sidecar mode.
    /// The renderer reads JSON-line IpcRequest messages from
    /// stdin, dispatches via the playback state machine, and
    /// writes JSON-line IpcResponse messages to stdout. The
    /// 7-op contract per spec §10. This slice ships the
    /// dispatcher + state-machine integration; slice (d) wires
    /// the actual GL paint to Advance's PaintSlide /
    /// PaintTransition results.
    #[arg(long, default_value_t = false)]
    ipc_sidecar: bool,

    /// v1-spec-delta #2 — render a synthesized text slide whose
    /// single layer has the given motion kind, exercising the
    /// per-frame animated render path on real scanout. One of
    /// `static` / `ticker` / `breathe` / `pulse` / `bounce` /
    /// `shake` / `blink`. Each kind held for `--hold-secs`
    /// (default 2 s); intensity / phase / speed default to spec
    /// midpoints (50 / 0.0 / 1.0). Smoke validates that
    /// render_animated_slide doesn't panic on real DRM hardware.
    #[arg(long)]
    play_motion_test: Option<String>,

    /// v1-spec-delta #2 (slice d) — animate a transition between
    /// two synthesized text slides, each with the given motion
    /// kind. Format: `--play-motion-transition KIND_A,KIND_B`.
    /// Drives the render_transition_animated per-frame FBO rebake
    /// path with both slides animated, validating that motion
    /// advances through transitions on real DRM scanout. Reuses
    /// `--transition` and `--transition-ms`.
    #[arg(long)]
    play_motion_transition: Option<String>,

    /// v1-spec-delta #3 (slice d) — render a synthesized auto_mode
    /// slide (one of `time` / `date` / `day`) for `--hold-secs`
    /// seconds. With `--hold-secs 5`, a `time` slide should tick
    /// the seconds digit 4-5 times during the hold. Validates the
    /// per-frame text re-rasterization on real DRM scanout.
    #[arg(long)]
    play_auto_mode_test: Option<String>,

    /// v1-spec-delta #6 (slice b+) -- render a synthesized slide
    /// with `background_pattern.pattern = KIND` and density 0.5.
    /// One of: `dots` / `halftone` / `stripes` / `scanlines` /
    /// `checker` / `grid` / `rings` / `rays` / `confetti` /
    /// `bricks`. Held for `--hold-secs` (default 2 s). Smoke gate
    /// validates the per-pattern shader compiles + draws on real
    /// DRM scanout. Each pattern's smoke phase asserts no panic +
    /// shader linked + bg drawn. Patterns whose shader hasn't
    /// landed yet warn-and-fall to color_a clear (still passes
    /// the no-panic gate; visual verification is per-slice).
    #[arg(long)]
    play_pattern_test: Option<String>,

    /// v1-spec-delta #7 (slice b+) -- render a synthesized slide
    /// with one text layer using the named blend mode against a
    /// solid bg. KIND is one of `normal` / `screen` / `multiply`
    /// / `overlay`. Held for `--hold-secs` (default 2). Smoke
    /// gate validates the per-layer blend func switch on real
    /// DRM scanout.
    #[arg(long)]
    play_blend_test: Option<String>,

    /// v1-spec-delta #8 (slice a+) -- render a PNG asset directly
    /// via the ImageSlide path. PATH is a filesystem path to a
    /// PNG; the smoke harness uploads a known asset to the Pi
    /// and runs `--play-image-slide /path/to/asset.png`. Hold for
    /// `--hold-secs` (default 2). Smoke gate validates the PNG
    /// decode + texture upload + FS_BLIT path on real DRM
    /// scanout.
    #[arg(long)]
    play_image_slide: Option<String>,

    /// v1-spec-delta #8 (F-image-bg-smoke) -- render a synthesized
    /// TextSlide whose `background_image_slide_id` references the
    /// given UUID, with a centered text layer composited on top.
    /// Requires `--content-root` to resolve `<root>/<UUID>/asset
    /// .png`. Validates the BgKind::Image path through paint_slide
    /// (decode + texture upload + FS_BLIT before glyph composite)
    /// on real DRM scanout, including the F-image-bg-cache
    /// session-wide reuse path.
    #[arg(long)]
    play_bg_image_test: Option<uuid::Uuid>,

    /// v1-spec-delta #4 (slice b/d) — render a synthesized slide
    /// with the layer's `outline` set. Validates that the
    /// FS_GLYPH_OUTLINE shader path links + draws on real DRM
    /// scanout. Visual diff vs --play-motion-test static is the
    /// presence of a 1-px black ring around the glyphs.
    #[arg(long, default_value_t = false)]
    play_outline_test: bool,

    /// Bug 3 Slice 1 prep (2026-05-19) -- run the glTexSubImage2D
    /// smoke test. Creates a 2048×2048 RGBA8 texture, writes a
    /// 48×48 sub-region via glTexSubImage2D, reads back via FBO,
    /// verifies pixel integrity. Exits with code 0 on PASS, non-
    /// zero on FAIL. Prints per-check report to stdout.
    #[arg(long, default_value_t = false)]
    gl_subtexture_smoke: bool,

    /// When `--play-reel` is set, loop the reel forever (wrapping
    /// from last slide back to first via the first slide's
    /// transition). Default: single pass.
    #[arg(long, default_value_t = false)]
    reel_loop: bool,

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
    /// Used by `--solid-color` / `--animate` / `--play-slide` /
    /// `--play-slide-text` / `--play-slide-via-fbo` / `--fade-from`
    /// (default 5s when not set). For `--play-reel` mode, leaving
    /// this unset uses each slide's `duration_ms` from the
    /// playlist; setting it explicitly overrides every slide's
    /// hold duration to the same value (handy for compressing
    /// smoke-test runtime).
    #[arg(long)]
    hold_secs: Option<u64>,

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

    /// qarl-direct perf-profile (2026-05-08): when set, enables
    /// per-frame phase timing + caps the animated render loop to
    /// N frames. Histogram dumps to stderr as a markdown table on
    /// exit. Use `--profile-frames 150` to capture ~5s of motion
    /// at the spec'd 30 fps target. Profile mode also SKIPS the
    /// fps-pacing sleep so we measure real shader-bound cadence,
    /// not vsync-padded.
    #[arg(long)]
    profile_frames: Option<u32>,

    /// v1-spec-delta #17 (2026-05-08): force a specific HDMI mode
    /// when EDID is missing/invalid (the dev Pi's safe-mode list
    /// tops out at 1024×768 absent EDID). Format: `WxH@Hz` like
    /// `1920x1080@30` or `1920x1080@60`. When unset, the renderer
    /// uses pick_largest_mode_index against the connector's
    /// reported modes (existing behavior). When set, the renderer
    /// synthesizes a CEA-861 mode with the given dimensions and
    /// uses it via SetCrtc -- this requires the panel to actually
    /// support the mode physically; an unsupported mode surfaces
    /// as a kernel SetCrtc error which the renderer logs + bails.
    #[arg(long)]
    force_mode: Option<String>,
}

/// v1-spec-delta #17: parsed --force-mode shape. `WxH@Hz` only;
/// flag suffixes (D, etc.) are not parsed today since they don't
/// change the kernel's mode validation outcome -- the kernel-side
/// timing-table match is what gates acceptance.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ForcedMode {
    pub width: u16,
    pub height: u16,
    pub vrefresh_hz: u32,
}

impl ForcedMode {
    /// Parse `WxH@Hz` -- width and height are u16 (drm cap), vrefresh
    /// is u32. Returns Err on any malformation rather than silently
    /// degrading to defaults; --force-mode that doesn't parse is a
    /// CLI bug, not a "fall back to EDID" intent.
    pub fn parse(s: &str) -> anyhow::Result<Self> {
        let (wh, hz) = s.split_once('@').ok_or_else(|| {
            anyhow::anyhow!("--force-mode {s:?} missing '@' (expected WxH@Hz like 1920x1080@30)")
        })?;
        let (w, h) = wh.split_once('x').ok_or_else(|| {
            anyhow::anyhow!("--force-mode {s:?} missing 'x' (expected WxH@Hz like 1920x1080@30)")
        })?;
        let width: u16 = w
            .parse()
            .map_err(|e| anyhow::anyhow!("--force-mode width {w:?}: {e}"))?;
        let height: u16 = h
            .parse()
            .map_err(|e| anyhow::anyhow!("--force-mode height {h:?}: {e}"))?;
        let vrefresh_hz: u32 = hz
            .parse()
            .map_err(|e| anyhow::anyhow!("--force-mode vrefresh {hz:?}: {e}"))?;
        if width == 0 || height == 0 || vrefresh_hz == 0 {
            anyhow::bail!("--force-mode {s:?} dimensions/vrefresh must all be > 0");
        }
        Ok(ForcedMode { width, height, vrefresh_hz })
    }
}

/// v1-spec-delta #2 (slice d-smoke) -- synthesize an in-memory
/// TextSlide with one motion layer of the given kind. Used by
/// `--play-motion-test KIND` to exercise the per-frame animated
/// render path on real scanout. Slide is plain (solid bg, single
/// centered text layer); only the `motion` field varies. Spec
/// midpoints (intensity=50, phase=0, speed=1.0) keep the curve
/// at canonical amplitude for visual inspection.
#[cfg(target_os = "linux")]
fn build_motion_test_slide(kind: &str) -> content::TextSlide {
    use uuid::Uuid;
    let layer = content::TextLayer {
        text: format!("MOTION {}", kind.to_uppercase()),
        name: String::new(),
        font_family: None,
        font_size_px: None,
        font_size_pct: Some(50.0),
        text_color: "#FFFFFF".to_string(),
        text_align: "center".to_string(),
        opacity: 1.0,
        visible: true,
        motion: kind.to_string(),
        motion_intensity: 50,
        motion_phase: 0.0,
        motion_speed: 1.0,
        auto_mode: None,
        auto_format: None,
        outline: false,
        drop_shadow: false,
        blend: "normal".to_string(),
        anchor: "center".to_string(),
        weight: None,
        r#box: content::TextBox {
            x: 0.05,
            y: 0.30,
            w: 0.90,
            h: 0.40,
        },
    };
    content::TextSlide {
        id: Uuid::nil(),
        name: format!("motion-test-{kind}"),
        duration_ms: 2000,
        background_color: "#1A1A1A".to_string(),
        background_pattern: None,
        background_image_slide_id: None,
        background_video_slide_id: None,
        text_layers: vec![layer],
    }
}

/// v1-spec-delta #3 (slice d) -- synthesize an in-memory TextSlide
/// with one auto_mode-set layer. Used by `--play-auto-mode-test
/// KIND` to exercise the per-frame clock/date substitution path
/// on real scanout. Default formats picked for visual prominence:
/// time -> time_hms (so seconds tick visibly), date -> date_long,
/// day -> day_long.
#[cfg(target_os = "linux")]
fn build_auto_mode_test_slide(kind: &str) -> content::TextSlide {
    use uuid::Uuid;
    let auto_format = match kind {
        "time" => "time_hms",
        "date" => "date_long",
        "day" => "day_long",
        _ => "time_hms",
    };
    let layer = content::TextLayer {
        text: format!("AUTO {}", kind.to_uppercase()),
        name: String::new(),
        font_family: None,
        font_size_px: None,
        font_size_pct: Some(50.0),
        text_color: "#FFFFFF".to_string(),
        text_align: "center".to_string(),
        opacity: 1.0,
        visible: true,
        motion: "static".to_string(),
        motion_intensity: 50,
        motion_phase: 0.0,
        motion_speed: 1.0,
        auto_mode: Some(kind.to_string()),
        auto_format: Some(auto_format.to_string()),
        outline: false,
        drop_shadow: false,
        blend: "normal".to_string(),
        anchor: "center".to_string(),
        weight: None,
        r#box: content::TextBox {
            x: 0.05,
            y: 0.30,
            w: 0.90,
            h: 0.40,
        },
    };
    content::TextSlide {
        id: Uuid::nil(),
        name: format!("auto-mode-test-{kind}"),
        duration_ms: 5000,
        background_color: "#1A1A1A".to_string(),
        background_pattern: None,
        background_image_slide_id: None,
        background_video_slide_id: None,
        text_layers: vec![layer],
    }
}

/// v1-spec-delta #4 (slice b/d) -- synthesize an in-memory
/// TextSlide with outline=true. Used by --play-outline-test to
/// exercise the FS_GLYPH_OUTLINE shader path on real DRM scanout.
#[cfg(target_os = "linux")]
/// v1-spec-delta #6 (slice b+) -- synthesize a slide whose
/// background is a single procedural pattern with the given
/// kind name + density 0.5. Color_a / color_b chosen so any
/// pattern shader produces a visible result on real hw (cyan +
/// orange; high contrast in both luma and chroma channels).
/// One text layer is added so the smoke can also assert text
/// composites correctly over the pattern (the FYS text layer
/// is the canonical user-facing content).
fn build_pattern_test_slide(pattern_name: &str) -> content::TextSlide {
    use uuid::Uuid;
    let layer = content::TextLayer {
        text: pattern_name.to_uppercase(),
        name: String::new(),
        font_family: None,
        font_size_px: None,
        font_size_pct: Some(40.0),
        text_color: "#FFFFFF".to_string(),
        text_align: "center".to_string(),
        opacity: 1.0,
        visible: true,
        motion: "static".to_string(),
        motion_intensity: 50,
        motion_phase: 0.0,
        motion_speed: 1.0,
        auto_mode: None,
        auto_format: None,
        outline: true,
        drop_shadow: false,
        blend: "normal".to_string(),
        anchor: "center".to_string(),
        weight: None,
        r#box: content::TextBox {
            x: 0.05,
            y: 0.40,
            w: 0.90,
            h: 0.20,
        },
    };
    content::TextSlide {
        id: Uuid::nil(),
        name: format!("pattern-test-{pattern_name}"),
        duration_ms: 2000,
        background_color: "#222222".to_string(),
        background_pattern: Some(content::BackgroundPattern {
            pattern: pattern_name.to_string(),
            color_a: "#00BFFF".to_string(),  // cyan
            color_b: "#FF6B00".to_string(),  // orange
            density: 0.5,
        }),
        background_image_slide_id: None,
        background_video_slide_id: None,
        text_layers: vec![layer],
    }
}

/// v1-spec-delta #8 (F-image-bg-smoke) -- synthesize a slide
/// whose background_image_slide_id references the given UUID.
/// One centered text layer composited on top so the smoke can
/// also assert text composites correctly over an image bg. The
/// asset is loaded from <content_root>/<UUID>/asset.png.
fn build_bg_image_test_slide(image_id: uuid::Uuid) -> content::TextSlide {
    use uuid::Uuid;
    let layer = content::TextLayer {
        text: "BG IMAGE".to_string(),
        name: String::new(),
        font_family: None,
        font_size_px: None,
        font_size_pct: Some(40.0),
        text_color: "#FFFFFF".to_string(),
        text_align: "center".to_string(),
        opacity: 1.0,
        visible: true,
        motion: "static".to_string(),
        motion_intensity: 50,
        motion_phase: 0.0,
        motion_speed: 1.0,
        auto_mode: None,
        auto_format: None,
        outline: true,
        drop_shadow: false,
        blend: "normal".to_string(),
        anchor: "center".to_string(),
        weight: None,
        r#box: content::TextBox {
            x: 0.05,
            y: 0.40,
            w: 0.90,
            h: 0.20,
        },
    };
    content::TextSlide {
        id: Uuid::nil(),
        name: format!("bg-image-test-{image_id}"),
        duration_ms: 2000,
        background_color: "#222222".to_string(),
        background_pattern: None,
        background_image_slide_id: Some(image_id),
        background_video_slide_id: None,
        text_layers: vec![layer],
    }
}

/// v1-spec-delta #7 (slice b+) -- synthesize a slide with one
/// text layer using the named blend mode. Layer text =
/// uppercased blend name; cyan text on orange bg so the blend
/// composite is visually distinct from "normal":
///   normal:   cyan text on orange bg.
///   multiply: dark color (cyan * orange).
///   screen:   light color (1 - (1-cyan)*(1-orange)).
///   overlay:  formula-dependent; slice (c) renders correctly,
///             slice (b) falls back to normal + emits warn.
fn build_blend_test_slide(blend_name: &str) -> content::TextSlide {
    use uuid::Uuid;
    let layer = content::TextLayer {
        text: blend_name.to_uppercase(),
        name: String::new(),
        font_family: None,
        font_size_px: None,
        font_size_pct: Some(50.0),
        text_color: "#00BFFF".to_string(),  // cyan
        text_align: "center".to_string(),
        opacity: 1.0,
        visible: true,
        motion: "static".to_string(),
        motion_intensity: 50,
        motion_phase: 0.0,
        motion_speed: 1.0,
        auto_mode: None,
        auto_format: None,
        outline: false,
        drop_shadow: false,
        blend: blend_name.to_string(),
        anchor: "center".to_string(),
        weight: None,
        r#box: content::TextBox {
            x: 0.05,
            y: 0.30,
            w: 0.90,
            h: 0.40,
        },
    };
    content::TextSlide {
        id: Uuid::nil(),
        name: format!("blend-test-{blend_name}"),
        duration_ms: 2000,
        background_color: "#FF6B00".to_string(),  // orange
        background_pattern: None,
        background_image_slide_id: None,
        background_video_slide_id: None,
        text_layers: vec![layer],
    }
}

fn build_outline_test_slide() -> content::TextSlide {
    use uuid::Uuid;
    let layer = content::TextLayer {
        text: "OUTLINE TEST".to_string(),
        name: String::new(),
        font_family: None,
        font_size_px: None,
        font_size_pct: Some(50.0),
        // Bright color so the outline ring is visually
        // distinguishable from the body fill.
        text_color: "#FFC700".to_string(),
        text_align: "center".to_string(),
        opacity: 1.0,
        visible: true,
        motion: "static".to_string(),
        motion_intensity: 50,
        motion_phase: 0.0,
        motion_speed: 1.0,
        auto_mode: None,
        auto_format: None,
        outline: true,
        drop_shadow: false,
        blend: "normal".to_string(),
        anchor: "center".to_string(),
        weight: None,
        r#box: content::TextBox {
            x: 0.05,
            y: 0.30,
            w: 0.90,
            h: 0.40,
        },
    };
    content::TextSlide {
        id: Uuid::nil(),
        // Mid-gray bg so the 1-px black outline is visible against
        // both the body fill and the bg.
        name: "outline-test".to_string(),
        duration_ms: 2000,
        background_color: "#666666".to_string(),
        background_pattern: None,
        background_image_slide_id: None,
        background_video_slide_id: None,
        text_layers: vec![layer],
    }
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
    use super::{parse_color, ForcedMode};

    // ============================================================
    // r73 (2026-06-06): preload warmup count regression lock.
    //
    // FYS 1080p workload before r73: every transition errored with
    // "feed sample 4: all 4 OUTPUT buffers in flight; drain wedged".
    // Root cause (confirmed via r70's [perf] v4l2_feed_drain_wait
    // instrumentation showing waited_us=102000 retries=25 on every
    // wedge): the r65 preload worker called prime_video_decoder()
    // which queued 1 primer + 3 warmup = 4 samples into a 4-slot
    // OUTPUT pool. Kernel decoded them into 4 CAPTURE buffers
    // (CAPTURE pool also 4) -- saturating CAPTURE. bcm2835-codec
    // can't release OUTPUT buffers back to userspace while the
    // CAPTURE queue is full. r73 reroutes the preload worker to
    // prime_video_decoder_with_warmup(dem, PRIME_WARMUP_FOR_PRELOAD)
    // = 0 warmups (primer only), leaving 3 of 4 CAPTURE slots free.
    //
    // These tests live in main.rs (binary crate root) NOT in
    // video_decode.rs (Linux-gated) so the constants get pinned on
    // macOS dev hosts too.
    // ============================================================

    #[test]
    fn prime_warmup_default_is_three() {
        // Cold-start callers (synchronous BeginSlide, standalone
        // reel) MUST keep warmup=3 -- the playback loop consumes
        // CAPTURE at 30fps so CAPTURE never saturates, and the
        // B-frame lookahead (typical H.264 reorder distance 3-4)
        // makes the difference between instant first-paint and a
        // ~100ms cold-start hiccup.
        assert_eq!(super::PRIME_WARMUP_DEFAULT, 3);
    }

    #[test]
    fn prime_warmup_for_preload_is_three() {
        // r93 (2026-06-08): bumped 2 -> 3 after the EOS-flush arc
        // (r80/r81/r82/r85/r87/r91/r92) closed empty. r92 proved
        // CMD_STOP is poisonous on bcm2835-codec; trying to FORCE
        // the kernel to emit a frame at low warmup isn't viable.
        // Simplest fix per QA r93 dispatch: feed more samples
        // (3) at preload, matching cold-start's known-working
        // value. See PRIME_WARMUP_FOR_PRELOAD docstring in main.rs
        // for the saturation-concern + roll-back-plan history.
        assert_eq!(super::PRIME_WARMUP_FOR_PRELOAD, 3);
    }

    #[test]
    fn preload_worker_call_site_uses_warmup_for_preload_constant() {
        // r73 regression lock against a future refactor that
        // accidentally reverts ipc_main.rs's preload worker back
        // to the cold-start-default `prime_video_decoder(&dem)`
        // call (which would re-introduce the FYS 1080p wedge).
        //
        // r76 Phase B (2026-06-07) update: preload now calls
        // `prime_video_decoder_for_preload` which wraps
        // `prime_video_decoder_with_warmup(PRIME_WARMUP_FOR_PRELOAD)`
        // PLUS drains the first CAPTURE frame to unblock the
        // bcm2835-codec wait-for-userspace state. The wrapper is
        // the canonical preload entry; the regression lock asserts
        // the preload site goes through it, not the bare prime.
        //
        // r73 subagent WARN-1 carried through: path-qualified call
        // expression with open-paren only appears in code, never in
        // prose comments.
        let src = include_str!("ipc_main.rs");
        assert!(
            src.contains("crate::video_decode::prime_video_decoder_for_preload("),
            "preload_in_worker MUST contain a path-qualified call to \
             `crate::video_decode::prime_video_decoder_for_preload(`. If you've \
             reverted to bare `prime_video_decoder(&dem)` OR \
             `prime_video_decoder_with_warmup(...)` directly, you've skipped \
             r76 Phase B's CAPTURE drain and re-introduced the \
             transition_endpoint_b_unconsumed regression -- see r76 commit \
             message."
        );
        // The wrapper itself MUST call
        // `prime_video_decoder_with_warmup(dem, PRIME_WARMUP_FOR_PRELOAD)`.
        // r76 subagent WARN-2: a bare `contains("PRIME_WARMUP_FOR_PRELOAD")`
        // is too weak -- the substring appears in the `pub use` re-export
        // and in docstrings, so a refactor that calls
        // `prime_video_decoder_with_warmup(dem, 0)` (or `with_warmup(dem, 3)`)
        // would still pass. Pin the full call expression with the constant
        // in argument position.
        let wrapper_src = include_str!("video_decode.rs");
        assert!(
            wrapper_src.contains("prime_video_decoder_with_warmup(dem, PRIME_WARMUP_FOR_PRELOAD)"),
            "prime_video_decoder_for_preload in video_decode.rs MUST contain the \
             exact call expression \
             `prime_video_decoder_with_warmup(dem, PRIME_WARMUP_FOR_PRELOAD)`. \
             If you've inlined a different warmup count (e.g. \
             `with_warmup(dem, 0)`), you've bypassed the r73 CAPTURE-saturation \
             guard."
        );
    }

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

    #[test]
    fn forced_mode_parses_canonical_1080p_30() {
        let m = ForcedMode::parse("1920x1080@30").unwrap();
        assert_eq!(m.width, 1920);
        assert_eq!(m.height, 1080);
        assert_eq!(m.vrefresh_hz, 30);
    }

    #[test]
    fn forced_mode_parses_1080p_60() {
        let m = ForcedMode::parse("1920x1080@60").unwrap();
        assert_eq!((m.width, m.height, m.vrefresh_hz), (1920, 1080, 60));
    }

    #[test]
    fn forced_mode_rejects_missing_at() {
        assert!(ForcedMode::parse("1920x1080").is_err());
    }

    #[test]
    fn forced_mode_rejects_missing_x() {
        assert!(ForcedMode::parse("1920@30").is_err());
    }

    #[test]
    fn forced_mode_rejects_zero_dim() {
        assert!(ForcedMode::parse("0x1080@30").is_err());
        assert!(ForcedMode::parse("1920x0@30").is_err());
        assert!(ForcedMode::parse("1920x1080@0").is_err());
    }

    #[test]
    fn forced_mode_rejects_non_numeric() {
        assert!(ForcedMode::parse("axb@c").is_err());
        assert!(ForcedMode::parse("1920x1080@30D").is_err());
    }

    #[test]
    fn forced_mode_rejects_empty() {
        assert!(ForcedMode::parse("").is_err());
        assert!(ForcedMode::parse("@").is_err());
    }
}

fn main() -> Result<()> {
    let args = Args::parse();

    // r87 (2026-06-09) startup env-var banner per QA r87 dispatch
    // ask. r86 telemetry showed EOS_FLUSH=1 systemd drop-in
    // caused REQBUFS EINVAL on the very first decoder open --
    // before any of my code's runtime branching could plausibly
    // diverge from the r84-clean OFF mode. Hypothesis: the
    // systemd drop-in inadvertently set OTHER env vars that
    // change the V4L2 init path (e.g. OPENMARQUEE_RENDERER_DMABUF).
    // r87 banner enumerates EVERY OPENMARQUEE_* env var visible
    // to this renderer subprocess at spawn time so QA can pin
    // the diff between OFF and ON modes from the journal.
    let mut renderer_env_vars: Vec<(String, String)> = std::env::vars()
        .filter(|(k, _)| k.starts_with("OPENMARQUEE_"))
        .collect();
    renderer_env_vars.sort_by(|a, b| a.0.cmp(&b.0));
    if renderer_env_vars.is_empty() {
        eprintln!("[perf] renderer_startup_env count=0");
    } else {
        for (k, v) in &renderer_env_vars {
            eprintln!("[perf] renderer_startup_env key={} value={:?}", k, v);
        }
    }

    // qarl-direct perf-profile: enable profile mode at startup
    // if --profile-frames is set. Renderer hot loops check
    // profile::is_enabled() / frames_remaining() to record
    // phase timings and stop after N frames. summarize() dumps
    // the histogram on the way out.
    if let Some(n) = args.profile_frames {
        profile::enable(n);
    }
    // Drop-guard so summarize() fires regardless of which
    // OutputMode branch returns. No-op when profile is disabled.
    struct ProfileGuard;
    impl Drop for ProfileGuard {
        fn drop(&mut self) {
            if profile::is_enabled() {
                profile::summarize();
            }
        }
    }
    let _profile_guard = ProfileGuard;

    // v1-spec-delta #17 (slice c): wire --force-mode CLI flag
    // through to hdmi.rs's process-wide OnceLock. Parse failures
    // surface here so the operator gets immediate feedback rather
    // than a deferred surprise inside DRM bring-up. set_forced_
    // mode is a no-op on non-Linux builds.
    let forced_mode = match args.force_mode.as_deref() {
        Some(s) => Some(ForcedMode::parse(s)?),
        None => None,
    };
    #[cfg(target_os = "linux")]
    hdmi::set_forced_mode(forced_mode);
    #[cfg(not(target_os = "linux"))]
    {
        let _ = forced_mode;
    }

    // SDF arc slice B: process-wide AA mode for MSDF fragment
    // shaders. Slice-D parity bake picks the empirical winner;
    // until then the operator can flip via --aa-mode.
    hdmi_logic::set_aa_mode(args.aa_mode);
    eprintln!("openmarquee-render: aa_mode={}", args.aa_mode.as_str());

    // v1-spec-delta #9 (slice c): IPC sidecar mode short-circuits
    // the standalone CLI dispatch. The dispatcher loop opens DRM
    // lazily via the Open op (slice (d)); slice (c) only handles
    // state-machine ops + emits OpResults derived from the
    // playback state. Cross-platform -- IPC sidecar mode is
    // useful in dev / CI where the Mac host doesn't have DRM.
    if args.ipc_sidecar {
        return ipc_main::run_ipc_sidecar();
    }

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
                    // v1-spec-delta #1: render_solid_color takes ms.
                    // CLI --hold-secs stays seconds for operator
                    // ergonomics; ×1000 here.
                    hdmi::render_solid_color(
                        &card,
                        color,
                        args.hold_secs.unwrap_or(5).saturating_mul(1000),
                    )?;
                    return Ok(());
                }
                if args.animate {
                    hdmi::render_animated_atomic(&card, args.hold_secs.unwrap_or(5), args.fps)?;
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
                        // v1-spec-delta #1: render_slide* take ms.
                        let hold_ms = args.hold_secs.unwrap_or(5).saturating_mul(1000);
                        hdmi::render_slide_via_fbo(&card, &slide, catalog_opt, args.content_root.as_deref(), hold_ms)
                    } else {
                        let hold_ms = args.hold_secs.unwrap_or(5).saturating_mul(1000);
                        hdmi::render_slide(&card, &slide, catalog_opt, args.content_root.as_deref(), hold_ms)
                    }
                };

                if args.play_reel {
                    let playlist_path = args
                        .playlist
                        .as_deref()
                        .unwrap_or_else(|| Path::new("/var/openmarquee/playlist.json"));
                    let content_root = args
                        .content_root
                        .as_deref()
                        .unwrap_or_else(|| Path::new("/var/openmarquee/content"));
                    let catalog = hdmi_logic::FontCatalog::new(
                        args.font_dir.clone(),
                        args.fallback_font_family.clone(),
                    );
                    let catalog_opt = if catalog.fallback_available() {
                        Some(&catalog)
                    } else {
                        eprintln!(
                            "warn: font catalog at {} can't load fallback {:?} \
                             — reel will render bg-only",
                            args.font_dir.display(),
                            args.fallback_font_family,
                        );
                        None
                    };
                    // hold-secs is Option<u64>: None means
                    // "use slide.duration_ms per item" (the
                    // production reel behavior); Some(N) means
                    // "override every slide to N seconds" (handy
                    // for compressed smoke-test runtime).
                    let override_hold = args.hold_secs;
                    hdmi::render_playlist_reel(
                        &card,
                        playlist_path,
                        content_root,
                        catalog_opt,
                        args.settings.as_deref(),
                        args.fps,
                        args.reel_loop,
                        override_hold,
                    )?;
                    return Ok(());
                }
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
                if let Some(kind) = args.play_motion_test.as_deref() {
                    // v1-spec-delta #2 -- synthesize an in-memory
                    // text slide with one layer of `kind` motion
                    // and render it through the standard
                    // render_slide path. Smoke gate for the
                    // per-frame animated render loop on real DRM
                    // hardware; FYS has no animated layers so this
                    // is the only on-Pi exercise of motion.
                    let catalog = hdmi_logic::FontCatalog::new(
                        args.font_dir.clone(),
                        args.fallback_font_family.clone(),
                    );
                    let catalog_opt = if catalog.fallback_available() {
                        Some(&catalog)
                    } else {
                        bail!(
                            "font catalog at {} can't load fallback {:?} -- needed for motion smoke",
                            args.font_dir.display(),
                            args.fallback_font_family,
                        );
                    };
                    let slide = build_motion_test_slide(kind);
                    let hold_ms = args.hold_secs.unwrap_or(2).saturating_mul(1000);
                    hdmi::render_slide(&card, &slide, catalog_opt, args.content_root.as_deref(), hold_ms)?;
                    return Ok(());
                }
                if let Some(spec) = args.play_motion_transition.as_deref() {
                    // v1-spec-delta #2 (slice d) -- exercise the
                    // per-frame transition rebake path. Synthesize
                    // two slides with each one's animated kind,
                    // run them through render_transition_animated
                    // with the operator-set --transition kind.
                    let parts: Vec<&str> = spec.split(',').collect();
                    if parts.len() != 2 {
                        bail!(
                            "--play-motion-transition expects KIND_A,KIND_B (got {spec:?})"
                        );
                    }
                    let catalog = hdmi_logic::FontCatalog::new(
                        args.font_dir.clone(),
                        args.fallback_font_family.clone(),
                    );
                    let catalog_opt = if catalog.fallback_available() {
                        Some(&catalog)
                    } else {
                        bail!(
                            "font catalog at {} can't load fallback {:?} -- needed for motion-transition smoke",
                            args.font_dir.display(),
                            args.fallback_font_family,
                        );
                    };
                    let slide_a = build_motion_test_slide(parts[0]);
                    let slide_b = build_motion_test_slide(parts[1]);
                    hdmi::render_transition_animated(
                        &card,
                        &slide_a,
                        &slide_b,
                        catalog_opt,
                        args.content_root.as_deref(),
                        &args.transition,
                        args.transition_ms,
                        args.fps,
                    )?;
                    return Ok(());
                }
                if args.gl_subtexture_smoke {
                    // Bug 3 Slice 1 prep (2026-05-19): verify
                    // glTexSubImage2D semantics on vc4 before
                    // committing the runtime glyph cache infra.
                    let report = gl_subtexture_smoke::run(&card)?;
                    print!("{}", gl_subtexture_smoke::format_report(&report));
                    if !report.all_pass() {
                        bail!("gl-subtexture-smoke FAIL");
                    }
                    return Ok(());
                }
                if let Some(pattern_name) = args.play_pattern_test.as_deref() {
                    // v1-spec-delta #6 (slice b+): synthesize a
                    // slide with the named procedural pattern at
                    // density 0.5 and render via the standard
                    // render_slide path. Smoke gate for the per-
                    // pattern fragment shader on real DRM hw.
                    let catalog = hdmi_logic::FontCatalog::new(
                        args.font_dir.clone(),
                        args.fallback_font_family.clone(),
                    );
                    let catalog_opt = if catalog.fallback_available() {
                        Some(&catalog)
                    } else {
                        bail!(
                            "font catalog at {} can't load fallback {:?} -- needed for pattern smoke",
                            args.font_dir.display(),
                            args.fallback_font_family,
                        );
                    };
                    let slide = build_pattern_test_slide(pattern_name);
                    let hold_ms = args.hold_secs.unwrap_or(2).saturating_mul(1000);
                    hdmi::render_slide(&card, &slide, catalog_opt, args.content_root.as_deref(), hold_ms)?;
                    return Ok(());
                }
                if let Some(slide_id) = args.capture_slide {
                    let png_path = args
                        .capture_path
                        .as_deref()
                        .ok_or_else(|| anyhow::anyhow!(
                            "--capture-slide requires --capture-path"
                        ))?;
                    let content_root = args
                        .content_root
                        .as_deref()
                        .ok_or_else(|| anyhow::anyhow!(
                            "--capture-slide requires --content-root"
                        ))?;
                    let catalog = hdmi_logic::FontCatalog::new(
                        args.font_dir.clone(),
                        args.fallback_font_family.clone(),
                    );
                    let catalog_opt = if catalog.fallback_available() {
                        Some(&catalog)
                    } else {
                        bail!(
                            "font catalog at {} can't load fallback {:?} -- needed for capture",
                            args.font_dir.display(),
                            args.fallback_font_family,
                        );
                    };
                    // Batch 18.1 / sweep #9 N2: dispatch on slide type.
                    // text_slide -> capture_slide_to_png (existing layered
                    // motion composite). image_slide -> capture_image_slide
                    // _to_png (asset.png blit, no text layers). video_slide
                    // -> capture against the asset.png THUMBNAIL (the
                    // VideoSlide storage spec puts the first frame at
                    // asset.png; renderer paints the same on slide entry
                    // before decode warm-up, so the thumbnail blit is the
                    // correct "first-frame" pixel signal for golden tests).
                    if let Some(slide) = content::find_text_slide(content_root, slide_id)? {
                        hdmi::capture_slide_to_png(
                            &card,
                            &slide,
                            catalog_opt,
                            Some(content_root),
                            png_path,
                            args.capture_slide_at_tick,
                        )?;
                    } else if content::find_image_slide(content_root, slide_id)?.is_some() {
                        let asset_path = content::image_slide_asset_path(
                            content_root, slide_id,
                        );
                        hdmi::capture_image_slide_to_png(
                            &card, &asset_path, png_path,
                        )?;
                    } else if content::find_video_slide(content_root, slide_id)?.is_some() {
                        // VideoSlide thumbnail lives at <id>/asset.png
                        // (the storage spec: "asset.png -- thumbnail
                        // (first frame, for saved-slides list)").
                        // video_slide_asset_path returns the .mp4; the
                        // thumbnail sibling is asset.png in the same
                        // dir. Construct inline.
                        let asset_path = content::video_slide_asset_path(
                            content_root, slide_id,
                        )
                        .with_file_name("asset.png");
                        hdmi::capture_image_slide_to_png(
                            &card, &asset_path, png_path,
                        )?;
                    } else {
                        bail!(
                            "no text_slide / image_slide / video_slide for \
                             {slide_id} under {}",
                            content_root.display()
                        );
                    }
                    return Ok(());
                }
                if args.capture_sb_mid {
                    // QA-direct (2026-05-09) visual-verdict capture
                    // path. Requires --fade-from, --fade-to,
                    // --transition, --capture-path, --content-root.
                    let png_path = args
                        .capture_path
                        .as_deref()
                        .ok_or_else(|| anyhow::anyhow!(
                            "--capture-sb-mid requires --capture-path"
                        ))?;
                    let content_root = args
                        .content_root
                        .as_deref()
                        .ok_or_else(|| anyhow::anyhow!(
                            "--capture-sb-mid requires --content-root"
                        ))?;
                    let from_id = args.fade_from.ok_or_else(|| anyhow::anyhow!(
                        "--capture-sb-mid requires --fade-from"
                    ))?;
                    let to_id = args.fade_to.ok_or_else(|| anyhow::anyhow!(
                        "--capture-sb-mid requires --fade-to"
                    ))?;
                    let catalog = hdmi_logic::FontCatalog::new(
                        args.font_dir.clone(),
                        args.fallback_font_family.clone(),
                    );
                    let catalog_opt = if catalog.fallback_available() {
                        Some(&catalog)
                    } else {
                        bail!(
                            "font catalog at {} can't load fallback {:?} -- needed for capture",
                            args.font_dir.display(),
                            args.fallback_font_family,
                        );
                    };
                    let slide_a = content::find_text_slide(content_root, from_id)?
                        .ok_or_else(|| anyhow::anyhow!(
                            "no text_slide for --fade-from {from_id} under {}",
                            content_root.display()
                        ))?;
                    let slide_b = content::find_text_slide(content_root, to_id)?
                        .ok_or_else(|| anyhow::anyhow!(
                            "no text_slide for --fade-to {to_id} under {}",
                            content_root.display()
                        ))?;
                    hdmi::capture_sb_transition_mid_to_png(
                        &card,
                        &slide_a,
                        &slide_b,
                        catalog_opt,
                        Some(content_root),
                        &args.transition,
                        args.capture_sb_t,
                        png_path,
                        args.capture_motion_tick,
                    )?;
                    return Ok(());
                }
                if args.capture_fullres_mid {
                    // QA-direct (2026-05-13) Atlas SB visual-sanity
                    // counterpart. Same CLI args as --capture-sb-mid;
                    // bakes both slides full-res into per-slide FBOs
                    // and composites with the SAME SP shader -- the
                    // PNG is the full-res reference baseline that SB
                    // half-rez output can be SSIM-diffed against.
                    let png_path = args
                        .capture_path
                        .as_deref()
                        .ok_or_else(|| anyhow::anyhow!(
                            "--capture-fullres-mid requires --capture-path"
                        ))?;
                    let content_root = args
                        .content_root
                        .as_deref()
                        .ok_or_else(|| anyhow::anyhow!(
                            "--capture-fullres-mid requires --content-root"
                        ))?;
                    let from_id = args.fade_from.ok_or_else(|| anyhow::anyhow!(
                        "--capture-fullres-mid requires --fade-from"
                    ))?;
                    let to_id = args.fade_to.ok_or_else(|| anyhow::anyhow!(
                        "--capture-fullres-mid requires --fade-to"
                    ))?;
                    let catalog = hdmi_logic::FontCatalog::new(
                        args.font_dir.clone(),
                        args.fallback_font_family.clone(),
                    );
                    let catalog_opt = if catalog.fallback_available() {
                        Some(&catalog)
                    } else {
                        bail!(
                            "font catalog at {} can't load fallback {:?} -- needed for capture",
                            args.font_dir.display(),
                            args.fallback_font_family,
                        );
                    };
                    let slide_a = content::find_text_slide(content_root, from_id)?
                        .ok_or_else(|| anyhow::anyhow!(
                            "no text_slide for --fade-from {from_id} under {}",
                            content_root.display()
                        ))?;
                    let slide_b = content::find_text_slide(content_root, to_id)?
                        .ok_or_else(|| anyhow::anyhow!(
                            "no text_slide for --fade-to {to_id} under {}",
                            content_root.display()
                        ))?;
                    hdmi::capture_fullres_transition_mid_to_png(
                        &card,
                        &slide_a,
                        &slide_b,
                        catalog_opt,
                        Some(content_root),
                        &args.transition,
                        args.capture_sb_t,
                        png_path,
                        args.capture_motion_tick,
                    )?;
                    return Ok(());
                }
                if let Some(image_id) = args.play_bg_image_test {
                    let catalog = hdmi_logic::FontCatalog::new(
                        args.font_dir.clone(),
                        args.fallback_font_family.clone(),
                    );
                    let catalog_opt = if catalog.fallback_available() {
                        Some(&catalog)
                    } else {
                        bail!(
                            "font catalog at {} can't load fallback {:?} -- needed for bg-image smoke",
                            args.font_dir.display(),
                            args.fallback_font_family,
                        );
                    };
                    let slide = build_bg_image_test_slide(image_id);
                    let hold_ms = args.hold_secs.unwrap_or(2).saturating_mul(1000);
                    hdmi::render_slide(
                        &card,
                        &slide,
                        catalog_opt,
                        args.content_root.as_deref(),
                        hold_ms,
                    )?;
                    return Ok(());
                }
                if let Some(asset_path) = args.play_image_slide.as_deref() {
                    let path = Path::new(asset_path);
                    let hold_ms = args.hold_secs.unwrap_or(2).saturating_mul(1000);
                    hdmi::render_image_slide(&card, path, hold_ms)?;
                    return Ok(());
                }
                if let Some(blend_name) = args.play_blend_test.as_deref() {
                    let catalog = hdmi_logic::FontCatalog::new(
                        args.font_dir.clone(),
                        args.fallback_font_family.clone(),
                    );
                    let catalog_opt = if catalog.fallback_available() {
                        Some(&catalog)
                    } else {
                        bail!(
                            "font catalog at {} can't load fallback {:?} -- needed for blend smoke",
                            args.font_dir.display(),
                            args.fallback_font_family,
                        );
                    };
                    let slide = build_blend_test_slide(blend_name);
                    let hold_ms = args.hold_secs.unwrap_or(2).saturating_mul(1000);
                    hdmi::render_slide(&card, &slide, catalog_opt, args.content_root.as_deref(), hold_ms)?;
                    return Ok(());
                }
                if args.play_outline_test {
                    // v1-spec-delta #4 (slice b/d): synthesize a
                    // slide with outline=true and render via the
                    // standard render_slide path. Smoke gate for
                    // FS_GLYPH_OUTLINE on real DRM hw.
                    let catalog = hdmi_logic::FontCatalog::new(
                        args.font_dir.clone(),
                        args.fallback_font_family.clone(),
                    );
                    let catalog_opt = if catalog.fallback_available() {
                        Some(&catalog)
                    } else {
                        bail!(
                            "font catalog at {} can't load fallback {:?} -- needed for outline smoke",
                            args.font_dir.display(),
                            args.fallback_font_family,
                        );
                    };
                    let slide = build_outline_test_slide();
                    let hold_ms = args.hold_secs.unwrap_or(2).saturating_mul(1000);
                    hdmi::render_slide(&card, &slide, catalog_opt, args.content_root.as_deref(), hold_ms)?;
                    return Ok(());
                }
                if let Some(kind) = args.play_auto_mode_test.as_deref() {
                    // v1-spec-delta #3 (slice d) -- synthesize an
                    // auto_mode slide and render. `kind` = time /
                    // date / day; format defaults to time_hms /
                    // date_long / day_long for visual prominence.
                    let catalog = hdmi_logic::FontCatalog::new(
                        args.font_dir.clone(),
                        args.fallback_font_family.clone(),
                    );
                    let catalog_opt = if catalog.fallback_available() {
                        Some(&catalog)
                    } else {
                        bail!(
                            "font catalog at {} can't load fallback {:?} -- needed for auto-mode smoke",
                            args.font_dir.display(),
                            args.fallback_font_family,
                        );
                    };
                    let slide = build_auto_mode_test_slide(kind);
                    let hold_ms = args.hold_secs.unwrap_or(5).saturating_mul(1000);
                    hdmi::render_slide(&card, &slide, catalog_opt, args.content_root.as_deref(), hold_ms)?;
                    return Ok(());
                }
                if let (Some(from_id), Some(to_id)) = (args.fade_from, args.fade_to) {
                    let content_root = args
                        .content_root
                        .as_deref()
                        .unwrap_or_else(|| Path::new("/var/openmarquee/content"));
                    let slide_a = content::find_text_slide(content_root, from_id)?
                        .ok_or_else(|| anyhow::anyhow!(
                            "no text_slide found for {from_id} under {}",
                            content_root.display(),
                        ))?;
                    let slide_b = content::find_text_slide(content_root, to_id)?
                        .ok_or_else(|| anyhow::anyhow!(
                            "no text_slide found for {to_id} under {}",
                            content_root.display(),
                        ))?;
                    let catalog = hdmi_logic::FontCatalog::new(
                        args.font_dir.clone(),
                        args.fallback_font_family.clone(),
                    );
                    let catalog_opt = if catalog.fallback_available() {
                        Some(&catalog)
                    } else {
                        eprintln!(
                            "warn: font catalog at {} can't load fallback {:?} \
                             — fade composite without text",
                            args.font_dir.display(),
                            args.fallback_font_family,
                        );
                        None
                    };
                    if args.animate_fade {
                        hdmi::render_transition_animated(
                            &card,
                            &slide_a,
                            &slide_b,
                            catalog_opt,
                            args.content_root.as_deref(),
                            &args.transition,
                            args.transition_ms,
                            args.fps,
                        )?;
                    } else {
                        // v1-spec-delta #1: render_fade_composite takes ms.
                        hdmi::render_fade_composite(
                            &card,
                            &slide_a,
                            &slide_b,
                            catalog_opt,
                            args.content_root.as_deref(),
                            args.fade_t,
                            args.hold_secs.unwrap_or(5).saturating_mul(1000),
                        )?;
                    }
                    return Ok(());
                }
                eprintln!("nothing to do — pass --probe, --solid-color R,G,B, --animate, --play-slide UUID, --play-slide-text UUID, --play-slide-via-fbo UUID, or --fade-from UUID --fade-to UUID");
            }
            #[cfg(not(target_os = "linux"))]
            {
                let _ = &args;
                bail!("--output hdmi requires Linux (drm/gbm/EGL); not available on this host");
            }
        }
        OutputMode::Mock => {
            // v1-spec-delta #11 (slice d) -- Mac dev preview
            // reach gate. Mock can dispatch to the snapshot-
            // capture primitive when --capture-slide is set
            // (since that path requires GLES2/EGL/DRM under
            // the hood, it's still Linux-only at runtime --
            // Mock just provides a separate CLI entrypoint
            // with cleaner diagnostics for callers that want
            // explicit "no scanout, capture only" semantics).
            // Future phase: a virtual-scanout PNG sequence
            // for full Mac dev preview.
            eprintln!(
                "mock output mode -- snapshot capture is the only Mac-side primitive today.\n\
                 Use --output hdmi --capture-slide UUID --capture-path PATH on Linux for a PNG\n\
                 snapshot. Per-frame paint preview on Mac is a future phase."
            );
        }
        OutputMode::Hub75 => {
            // v1-spec-delta #11 (slice e) -- HUB75 64x64 RGB
            // matrix reach gate per spec §5. Panel-write path
            // (rgb-matrix-rs / vc4-side bit-banging /
            // dedicated controller subprocess) is post-v1.
            eprintln!(
                "hub75 output -- reach gate only; panel-write path stubbed for v1.\n\
                 Spec §5: 64x64 RGB matrix at 30 fps over the GPIO HUB75 driver.\n\
                 This binary build accepts --output hub75 and exits cleanly so the\n\
                 backend can detect the renderer's reach without panicking on a\n\
                 not-yet-implemented arm."
            );
        }
        OutputMode::Ws2812b => {
            // v1-spec-delta #11 (slice e) -- WS2812B serial-
            // RGB strip reach gate per spec §5. Panel-write
            // path (DMA-driven 800kHz serial protocol /
            // dedicated subprocess) is post-v1.
            eprintln!(
                "ws2812b output -- reach gate only; panel-write path stubbed for v1.\n\
                 Spec §5: WS2812B serial-RGB strip with DMA at 800 kHz.\n\
                 This binary build accepts --output ws2812b and exits cleanly so the\n\
                 backend can detect the renderer's reach without panicking on a\n\
                 not-yet-implemented arm."
            );
        }
    }

    Ok(())
}
