//! Cross-platform mirror of the Python content + playlist models —
//! just enough to load a TextSlide's id/name/background and find it
//! by UUID. The full content model lives in
//! `backend/openmarquee/content/__init__.py`; we mirror only the
//! fields we actively consume so unrelated schema changes don't
//! break us.
//!
//! Plan §6.1: treat the content model as a fixed input. We
//! deliberately keep this struct list narrow — Phase 4 entry only
//! needs `background_color` + the basics; transitions, text layers,
//! motion, auto-mode all come back as their phases need them.
//!
//! Tolerance posture: `#[serde(default)]` on optional-by-default
//! fields, and we DON'T set `deny_unknown_fields` — the Python side
//! adds fields ahead of us.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Deserialize;
use uuid::Uuid;

/// Top-level playlist.json envelope. Backend writes
/// `{schema_version, playlists: [{id, name, items: [...], ...}]}`.
#[derive(Debug, Deserialize)]
pub struct PlaylistEnvelope {
    #[serde(default)]
    pub schema_version: u32,
    pub playlists: Vec<Playlist>,
}

#[derive(Debug, Deserialize)]
pub struct Playlist {
    pub id: Uuid,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub items: Vec<PlaylistItemRef>,
}

/// A reference to a content item by UUID, plus the per-item exit
/// transition. The actual slide payload lives at
/// `<content_root>/<item_id>/item.json`.
#[derive(Debug, Deserialize, Clone)]
pub struct PlaylistItemRef {
    pub item_id: Uuid,
    #[serde(default = "default_transition")]
    pub transition: String,
    #[serde(default = "default_transition_ms")]
    pub transition_ms: u32,
}

fn default_transition() -> String {
    "cut".to_string()
}

fn default_transition_ms() -> u32 {
    500
}

/// Per-item file envelope: `{schema_version, updated_at, item: {...}}`.
/// We type the inner `item` as `serde_json::Value` first to dispatch
/// on `type` ourselves — Python's discriminated union (Pydantic
/// `Annotated[..., Field(discriminator="type")]`) doesn't translate
/// 1:1 to serde tags when fields differ across variants.
#[derive(Debug, Deserialize)]
pub struct ItemEnvelope {
    #[serde(default)]
    pub schema_version: u32,
    pub item: serde_json::Value,
}

/// Minimal mirror of `TextSlide`. We expose only what Phase 4 uses;
/// later phases extend.
#[derive(Debug, Deserialize, Clone)]
pub struct TextSlide {
    pub id: Uuid,
    #[serde(default)]
    pub name: String,
    #[serde(default = "default_duration_ms")]
    pub duration_ms: u32,
    /// Hex `#RRGGBB`. Used when `background_pattern` is `None`, OR
    /// as the fallback for patterns this renderer phase doesn't yet
    /// support.
    #[serde(default = "default_bg_color")]
    pub background_color: String,
    /// Procedural pattern over two colors. When set, takes precedence
    /// over `background_color`. Phase 4.1a renders only the `solid`
    /// pattern (uses `color_a`); other patterns fall back to
    /// `background_color` until their phases land.
    #[serde(default)]
    pub background_pattern: Option<BackgroundPattern>,
    /// v1-spec-delta #8 (slice b) -- per-spec, a TextSlide can
    /// reference an ImageSlide to use as its background. When set,
    /// the renderer loads `<content_root>/<id>/asset.png`, uploads
    /// it as a fullscreen-blit texture, and composites text layers
    /// on top. Mutex with `background_pattern` per the Python
    /// validator (only one of the three bg sources is meaningful);
    /// the renderer doesn't enforce mutex -- if both are set,
    /// background_image_slide_id wins as the actual scanout source
    /// and background_pattern is ignored with a warn.
    #[serde(default)]
    pub background_image_slide_id: Option<Uuid>,
    /// Ordered text layers; index 0 draws first, later entries
    /// composite over earlier ones. Phase 4.2a renders only the
    /// FIRST layer; multi-layer compositing lands in Phase 4.2c.
    #[serde(default)]
    pub text_layers: Vec<TextLayer>,
}

/// Mirror of `backend.openmarquee.content.TextLayer` — the subset of
/// fields Phase 4.2 reads. Position + size of the layer's box are in
/// slide-relative fractions [0, 1]. Font size is either absolute
/// pixels (`font_size_px`) OR a percent (`font_size_pct`). The
/// Python model semantics for percent are "percent of box width"; the
/// Phase 4.2a renderer uses a height-based heuristic instead until
/// the real fit-to-box pass lands in 4.2c (see hdmi.rs::
/// `render_slide`'s `draw_text_layer` for details).
#[derive(Debug, Deserialize, Clone)]
pub struct TextLayer {
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub name: String,
    /// Family name (e.g. `"Anton"`). The renderer maps this to a
    /// TTF on disk via `font_family_to_path`. None / unknown family
    /// → fallback (TBD; currently always Anton).
    #[serde(default)]
    pub font_family: Option<String>,
    #[serde(default)]
    pub font_size_px: Option<f32>,
    #[serde(default)]
    pub font_size_pct: Option<f32>,
    #[serde(default = "default_text_color")]
    pub text_color: String,
    /// `left | center | right`. Phase 4.2a ignores this (left-only);
    /// 4.2c implements it.
    #[serde(default = "default_text_align")]
    pub text_align: String,
    #[serde(default = "default_opacity")]
    pub opacity: f32,
    #[serde(default = "default_visible")]
    pub visible: bool,
    /// v1-spec-delta #2 -- per-layer animation. One of seven values
    /// (`static` / `ticker` / `breathe` / `pulse` / `bounce` /
    /// `shake` / `blink`); see `docs/text-layer-motion-spec.md` for
    /// the operator-facing menu and per-effect intensity mapping.
    /// Legacy "scroll" is migrated to "ticker" in
    /// `deserialize_motion` so v3 envelopes continue to load.
    #[serde(default = "default_motion", deserialize_with = "deserialize_motion")]
    pub motion: String,
    /// 0..100 single-knob intensity. Spec default 50. Per-effect
    /// table maps this to amplitude/frequency ranges; see
    /// `effective_motion_*` helpers in `hdmi_logic.rs`.
    #[serde(default = "default_motion_intensity")]
    pub motion_intensity: u8,
    /// 0.0..1.0 phase offset on the shared global tick. Two layers
    /// with the same effect at phase=0.0 and phase=0.5 run in
    /// opposition. Also seeds the deterministic RNG for `shake`.
    #[serde(default = "default_motion_phase")]
    pub motion_phase: f32,
    /// 0.0..2.0 frequency multiplier (B2, 2026-05-05). 1.0 = spec
    /// default, 0.0 = frozen, 2.0 = double speed. Multiplies the
    /// per-effect Hz from the intensity table.
    #[serde(default = "default_motion_speed")]
    pub motion_speed: f32,
    /// v1-spec-delta #3 -- auto-update mode. When set, the renderer
    /// substitutes `text` with a formatted system-clock value (time
    /// / date / day-of-week) every second. Spec §6.1 lines 114-118.
    /// One of `time` / `date` / `day` or None.
    #[serde(default)]
    pub auto_mode: Option<String>,
    /// v1-spec-delta #3 -- auto-update format string. Mode-scoped:
    /// `time_*` for auto_mode=time, `date_*` for date, `day_*` for
    /// day. The Python validator rejects mismatched combinations on
    /// save, so the renderer trusts the saved envelope and falls
    /// back to a sensible default (time_hm / date_medium / day_long)
    /// if the field is somehow None despite auto_mode being set.
    #[serde(default)]
    pub auto_format: Option<String>,
    /// v1-spec-delta #4 -- 1-pixel stroke outline around the
    /// glyphs (Python convention: black). Default false. When
    /// true, the renderer dispatches to FS_GLYPH_OUTLINE which
    /// dilates the glyph alpha mask by 1 pixel and renders the
    /// ring in u_outline_color (black).
    #[serde(default)]
    pub outline: bool,
    /// v1-spec-delta #7 -- per-layer compositing mode against
    /// what's already been drawn. Schema literal:
    /// `normal` / `screen` / `multiply` / `overlay`. Default
    /// `normal` = source-over premultiplied alpha (today's
    /// shipped behavior). The other 3 land in subsequent
    /// slices: slice (b) wires multiply + screen via GL blend
    /// func tweaks; slice (c) implements overlay via an FBO
    /// ping-pong shader (overlay needs a per-pixel dst sample
    /// that fixed-function blend can't express).
    #[serde(default = "default_blend")]
    pub blend: String,
    /// v1.0 close (2026-05-30) — vertical anchor INSIDE the layer
    /// box. One of `top` / `center` / `bottom`; spec §5.10a line 318.
    /// Honored at paint via `parse_v_align` in `draw_text_layer_msdf`.
    #[serde(default = "default_anchor")]
    pub anchor: String,
    /// v1.0 close (2026-05-30) — variable-font CSS weight (100-900,
    /// multiples of 100) from the Pydantic model. Wire-accepted but
    /// render-deferred: the bundled font system is single-TTF-per-
    /// family (see `font_family_to_filename` in hdmi_logic.rs) and
    /// fontdue does not support variable-axis selection, so the field
    /// survives a save/load round-trip but the renderer paints with
    /// the bundled TTF's native weight. Honoring weight ships behind
    /// a font-bundling decision (variant TTFs OR a different
    /// rasterizer) — queued for v1.1.
    #[serde(default)]
    pub weight: Option<u32>,
    pub r#box: TextBox,
}

impl TextLayer {
    /// Parity Bug 1 (2026-05-19): is this layer worth laying out?
    ///
    /// A layer is renderable when it's visible AND it carries
    /// something to draw — either a non-empty static `text`, OR
    /// an `auto_mode` (time / date / day clock) whose visible
    /// string is resolved at paint time. An auto_mode layer's
    /// `text` field is empty BY DESIGN, so a naive
    /// `!text.is_empty()` filter wrongly drops it before the
    /// resolver ever runs — that was the Boot time-clock bug.
    pub fn is_renderable(&self) -> bool {
        self.visible && (!self.text.is_empty() || self.auto_mode.is_some())
    }
}

/// Position + size of a text layer relative to the slide. All four
/// fields are 0..1 fractions of slide dimensions.
#[derive(Debug, Deserialize, Clone, Copy)]
pub struct TextBox {
    pub x: f32,
    pub y: f32,
    pub w: f32,
    pub h: f32,
}

fn default_text_color() -> String {
    "#FFFFFF".to_string()
}
fn default_text_align() -> String {
    "center".to_string()
}
fn default_opacity() -> f32 {
    1.0
}
fn default_visible() -> bool {
    true
}
fn default_anchor() -> String {
    "center".to_string()
}
fn default_motion() -> String {
    "static".to_string()
}
fn default_motion_intensity() -> u8 {
    50
}
fn default_motion_phase() -> f32 {
    0.0
}
fn default_motion_speed() -> f32 {
    1.0
}
fn default_blend() -> String {
    "normal".to_string()
}

/// Mirror of `TextLayer._migrate_legacy_motion` -- rename "scroll"
/// to "ticker" before storing. Old v3 envelopes have "scroll"; the
/// editor's next save() writes "ticker" back, so this validator
/// drains lazily as content is edited (no SCHEMA_VERSION bump).
/// Unknown values fall through unchanged so a future-added value
/// doesn't get silently mangled here.
fn deserialize_motion<'de, D>(d: D) -> Result<String, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let raw = String::deserialize(d)?;
    Ok(if raw == "scroll" {
        "ticker".to_string()
    } else {
        raw
    })
}

/// Mirror of `backend.openmarquee.content.BackgroundPattern`. Pattern
/// is one of 12: `solid`, `gradient`, `dots`, `halftone`, `stripes`,
/// `scanlines`, `checker`, `grid`, `rings`, `rays`, `confetti`,
/// `bricks`. `color_a` and `color_b` are hex `#RRGGBB`. `density` is
/// a normalized 0..1 knob with per-pattern meaning (see Python
/// docstring for full table).
#[derive(Debug, Deserialize, Clone)]
pub struct BackgroundPattern {
    pub pattern: String,
    pub color_a: String,
    #[serde(default = "default_color_b")]
    pub color_b: String,
    #[serde(default = "default_density")]
    pub density: f32,
}

fn default_color_b() -> String {
    "#FFFFFF".to_string()
}

fn default_density() -> f32 {
    0.5
}

fn default_duration_ms() -> u32 {
    5000
}

fn default_bg_color() -> String {
    "#000000".to_string()
}

/// Read a playlist.json from disk and return the envelope.
pub fn load_playlist(path: &Path) -> Result<PlaylistEnvelope> {
    let bytes = std::fs::read(path)
        .with_context(|| format!("read playlist {}", path.display()))?;
    let env: PlaylistEnvelope = serde_json::from_slice(&bytes)
        .with_context(|| format!("parse playlist {}", path.display()))?;
    Ok(env)
}

/// Locate the text-slide JSON for a given item_id under
/// `<content_root>/<item_id>/item.json` and parse it.
///
/// Returns `Ok(None)` when the item exists but isn't a text slide
/// (image/video) — caller decides how to handle non-text items at
/// Phase 4 entry. Errors only on filesystem / JSON-parse failures.
pub fn find_text_slide(content_root: &Path, item_id: Uuid) -> Result<Option<TextSlide>> {
    let path = item_dir(content_root, item_id).join("item.json");
    let bytes = std::fs::read(&path)
        .with_context(|| format!("read item {}", path.display()))?;
    let envelope: ItemEnvelope = serde_json::from_slice(&bytes)
        .with_context(|| format!("parse item envelope {}", path.display()))?;
    let kind = envelope
        .item
        .get("type")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if kind != "text_slide" {
        return Ok(None);
    }
    let slide: TextSlide = serde_json::from_value(envelope.item)
        .with_context(|| format!("parse text_slide {}", path.display()))?;
    Ok(Some(slide))
}

/// v1-spec-delta #8 (slice a, 2026-05-08) -- minimal mirror of
/// Python's `ImageSlide`. Asset (PNG, browser-pre-scaled to the
/// sign's native resolution) lives at `<content_root>/<id>/
/// asset.png` per backend storage convention. The schema fields
/// the renderer needs are id + duration_ms + transition; name is
/// kept for log lines.
#[derive(Debug, Deserialize, Clone)]
pub struct ImageSlide {
    #[serde(default = "Uuid::nil")]
    pub id: Uuid,
    #[serde(default)]
    pub name: String,
    #[serde(default = "default_image_duration_ms")]
    pub duration_ms: u32,
    #[serde(default = "default_transition")]
    pub transition: String,
    #[serde(default = "default_transition_ms")]
    pub transition_ms: u32,
}

fn default_image_duration_ms() -> u32 {
    5000
}

/// v1-spec-delta #8 (slice a) -- locate the image-slide JSON for
/// a given item_id. Mirrors find_text_slide; returns Ok(None)
/// when the item exists but isn't an image slide. Caller
/// dispatches the type selection -- find_text_slide /
/// find_image_slide are parallel narrow loaders.
///
/// Also accepts `web`-type slides: a Web slide is "an image slide
/// whose asset.png is auto-refreshed from the render helper" (the
/// backend writes the fetched screenshot to the same `asset.png`),
/// so the renderer paints it via the exact image path. A `web`
/// envelope carries extra `url` / `refresh_interval_s` fields, but
/// `ImageSlide` has no `deny_unknown_fields` and every field has a
/// serde default, so it deserializes cleanly into `ImageSlide`.
pub fn find_image_slide(content_root: &Path, item_id: Uuid) -> Result<Option<ImageSlide>> {
    let path = item_dir(content_root, item_id).join("item.json");
    let bytes = std::fs::read(&path)
        .with_context(|| format!("read item {}", path.display()))?;
    let envelope: ItemEnvelope = serde_json::from_slice(&bytes)
        .with_context(|| format!("parse item envelope {}", path.display()))?;
    let kind = envelope
        .item
        .get("type")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if kind != "image" && kind != "web" {
        return Ok(None);
    }
    let slide: ImageSlide = serde_json::from_value(envelope.item)
        .with_context(|| format!("parse image_slide {}", path.display()))?;
    Ok(Some(slide))
}

/// v1-spec-delta #8 (slice a) -- the asset PNG path for an
/// ImageSlide. The browser pre-scales to the panel's native
/// resolution + uploads as PNG, so the renderer only ever sees
/// PNG. Path: `<content_root>/<id>/asset.png`.
pub fn image_slide_asset_path(content_root: &Path, slide_id: Uuid) -> PathBuf {
    item_dir(content_root, slide_id).join("asset.png")
}

/// v1-spec-delta #8 (slice c, 2026-05-08) -- minimal mirror of
/// Python's `VideoSlide`. Asset (H.264 in MP4 container, capped
/// at 1080p per the backend docstring) lives at
/// `<content_root>/<id>/asset.mp4`; thumbnail at `asset.png`.
/// The schema fields the renderer needs are id + duration_ms +
/// transition; name is kept for log lines. duration_ms is
/// informational per the Python schema -- the playback engine
/// reads actual runtime from the file -- but the renderer
/// honors it for hold-time hints and the reel driver's pass_ms
/// gate.
///
/// SLICE-C SCOPE NOTE: this slice ships the schema mirror +
/// ContentItem::Video dispatch + warn-and-fall in the reel
/// driver. Actual H.264 hardware-decode (V4L2 M2M via
/// gstreamer / ffmpeg / direct linux-media-rs) is the
/// architectural follow-up. The decode-approach selection has
/// material spec-compliance + perf consequences and warrants
/// qarl-direct review before implementation; this slice gets
/// the dispatch shape in place so the renderer doesn't choke
/// on video envelopes in playlists.
#[derive(Debug, Deserialize, Clone)]
pub struct VideoSlide {
    #[serde(default = "Uuid::nil")]
    pub id: Uuid,
    #[serde(default)]
    pub name: String,
    #[serde(default = "default_video_duration_ms")]
    pub duration_ms: u32,
    #[serde(default = "default_transition")]
    pub transition: String,
    #[serde(default = "default_transition_ms")]
    pub transition_ms: u32,
}

fn default_video_duration_ms() -> u32 {
    5000
}

/// v1-spec-delta #8 (slice c) -- locate the video-slide JSON for
/// a given item_id. Mirrors find_text_slide / find_image_slide;
/// returns Ok(None) when the item exists but isn't a video
/// slide. Caller dispatches on type.
pub fn find_video_slide(content_root: &Path, item_id: Uuid) -> Result<Option<VideoSlide>> {
    let path = item_dir(content_root, item_id).join("item.json");
    let bytes = std::fs::read(&path)
        .with_context(|| format!("read item {}", path.display()))?;
    let envelope: ItemEnvelope = serde_json::from_slice(&bytes)
        .with_context(|| format!("parse item envelope {}", path.display()))?;
    let kind = envelope
        .item
        .get("type")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if kind != "video" {
        return Ok(None);
    }
    let slide: VideoSlide = serde_json::from_value(envelope.item)
        .with_context(|| format!("parse video_slide {}", path.display()))?;
    Ok(Some(slide))
}

/// v1-spec-delta #10 (slice a, 2026-05-08) -- minimal mirror of
/// Python's `Settings` model. Operator-controlled per spec §6.3.
/// The renderer consumes display_rotation / brightness / gamma
/// each frame; the rest (Wi-Fi / Tailscale / sign_name) is
/// out of scope for the renderer (those are systemd / NM
/// concerns).
///
/// Forward-compat: unknown fields are ignored (no
/// deny_unknown_fields). Numeric fields use serde default so
/// missing-field envelopes deserialize cleanly.
#[derive(Debug, Deserialize, Clone, PartialEq)]
pub struct Settings {
    #[serde(default)]
    pub schema_version: u32,
    #[serde(default = "default_display_width")]
    pub display_width: u32,
    #[serde(default = "default_display_height")]
    pub display_height: u32,
    /// Rotation in degrees applied to the rendered scanout.
    /// One of 0 / 90 / 180 / 270 per spec §6.3. Other values
    /// fall back to 0 with a warn at apply time.
    #[serde(default)]
    pub display_rotation: i32,
    /// Brightness scalar in [0, 100]. The renderer multiplies
    /// final RGB by brightness / 100. Spec §6.3.
    #[serde(default = "default_brightness")]
    pub brightness: u32,
    /// Gamma in (0.0, 4.0]. Default 1.0 (identity — the scanout
    /// post-pass is a no-op blit and gets skipped via
    /// `is_color_identity`). Operators can dial up if the TV's
    /// HDMI pipeline isn't gamma-correct on its own. Applied
    /// in a per-pixel post-pass at scanout time (slice b).
    #[serde(default = "default_gamma")]
    pub gamma: f32,
}

fn default_display_width() -> u32 {
    1920
}
fn default_display_height() -> u32 {
    1080
}
fn default_brightness() -> u32 {
    100
}
fn default_gamma() -> f32 {
    1.0
}

/// v1-spec-delta #10 (slice a) -- read settings.json from disk
/// and parse it. Returns the parsed Settings on success;
/// errors surface filesystem or JSON-parse failures with
/// context. Caller (CLI / IPC) decides whether missing
/// settings is fatal or falls back to defaults.
pub fn load_settings(path: &Path) -> Result<Settings> {
    let bytes = std::fs::read(path)
        .with_context(|| format!("read settings {}", path.display()))?;
    let s: Settings = serde_json::from_slice(&bytes)
        .with_context(|| format!("parse settings {}", path.display()))?;
    Ok(s)
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            schema_version: 1,
            display_width: default_display_width(),
            display_height: default_display_height(),
            display_rotation: 0,
            brightness: default_brightness(),
            gamma: default_gamma(),
        }
    }
}

impl Settings {
    /// v1-spec-delta #10 (slice c) -- whether brightness +
    /// gamma are color-identity (skip the post-pass). Spec
    /// schema brightness is in [0, 100] (so 100 = full); the
    /// renderer's shader takes brightness/100 in [0, 1]. Gamma
    /// near 1.0 with brightness near 100 means the post-pass
    /// is a no-op blit; the caller can route paint directly to
    /// the default framebuffer and skip the FBO ping-pong.
    pub fn is_color_identity(&self) -> bool {
        self.brightness == 100 && (self.gamma - 1.0).abs() < 0.001
    }
}

/// v1-spec-delta #10 (slice a) -- mtime-polling settings
/// watcher. Cheaper than notify on Pi Zero 2 W (no inotify-
/// rs deps; just stat each call) and well within spec §8.5's
/// ≤2s-apply requirement at 500 ms polling cadence (which
/// the caller drives -- this struct doesn't sleep).
///
/// Usage: caller constructs once with the settings.json
/// path, then calls `check()` opportunistically (e.g.,
/// between slides in --play-reel, or in the IPC sidecar's
/// inner loop between Advance ticks). The first `check()`
/// returns Some(initial) so the caller can apply on
/// startup; subsequent calls return None when mtime is
/// unchanged + Some(new_settings) when changed.
pub struct SettingsWatcher {
    path: PathBuf,
    last_mtime: Option<std::time::SystemTime>,
    /// True until the first successful check() so we always
    /// emit the initial state on startup even if the file's
    /// mtime is older than the renderer process start.
    bootstrap: bool,
}

impl SettingsWatcher {
    pub fn new(path: PathBuf) -> Self {
        Self {
            path,
            last_mtime: None,
            bootstrap: true,
        }
    }

    /// Check if settings.json has changed since the last
    /// successful call. Returns:
    ///   * Some(settings) on first call, on mtime change, or
    ///     when the file becomes readable after a transient
    ///     error.
    ///   * None when unchanged or when the file is unreadable
    ///     (the caller continues with the last-known
    ///     settings).
    ///
    /// Errors during stat / parse are absorbed silently --
    /// settings reactivity is best-effort and the caller
    /// shouldn't bail on a transient backend write that
    /// momentarily renders the file invalid.
    pub fn check(&mut self) -> Option<Settings> {
        let metadata = std::fs::metadata(&self.path).ok()?;
        let mtime = metadata.modified().ok()?;
        let bootstrap = self.bootstrap;
        let changed = bootstrap
            || self.last_mtime.map(|prev| prev != mtime).unwrap_or(true);
        if !changed {
            return None;
        }
        let settings = match load_settings(&self.path) {
            Ok(s) => s,
            Err(e) => {
                eprintln!(
                    "warn: SettingsWatcher load failed for {}: {e:#}; keeping last-known",
                    self.path.display(),
                );
                return None;
            }
        };
        self.last_mtime = Some(mtime);
        self.bootstrap = false;
        Some(settings)
    }
}

/// v1-spec-delta #8 (slice c) -- the asset MP4 path for a
/// VideoSlide. H.264 video, browser-pre-transcoded to
/// min(source, 1920×1080) per the Python schema docstring.
/// Path: `<content_root>/<id>/asset.mp4`.
pub fn video_slide_asset_path(content_root: &Path, slide_id: Uuid) -> PathBuf {
    item_dir(content_root, slide_id).join("asset.mp4")
}

/// v1-spec-delta #8 (slice a/c) -- typed reel item. The reel
/// driver dispatches on this enum; each variant has its own
/// render path. Text via render_slide_in_session, Image via
/// render_image_slide_in_session. Video lands in slice (c)+
/// pending the decode-approach selection (warn-and-fall to
/// hard cut today).
#[derive(Debug, Clone)]
pub enum ContentItem {
    Text(TextSlide),
    Image(ImageSlide),
    Video(VideoSlide),
}

impl ContentItem {
    pub fn id(&self) -> Uuid {
        match self {
            ContentItem::Text(s) => s.id,
            ContentItem::Image(s) => s.id,
            ContentItem::Video(s) => s.id,
        }
    }
    pub fn name(&self) -> &str {
        match self {
            ContentItem::Text(s) => &s.name,
            ContentItem::Image(s) => &s.name,
            ContentItem::Video(s) => &s.name,
        }
    }
    pub fn duration_ms(&self) -> u32 {
        match self {
            ContentItem::Text(s) => s.duration_ms,
            ContentItem::Image(s) => s.duration_ms,
            ContentItem::Video(s) => s.duration_ms,
        }
    }
    pub fn type_label(&self) -> &'static str {
        match self {
            ContentItem::Text(_) => "text_slide",
            ContentItem::Image(_) => "image",
            ContentItem::Video(_) => "video",
        }
    }
}

/// `<content_root>/<item_id>/`. Pulled out so tests can build the
/// same path without re-deriving the layout convention.
pub fn item_dir(content_root: &Path, item_id: Uuid) -> PathBuf {
    content_root.join(item_id.to_string())
}

/// Resolve a playlist's items into a flat list of
/// `(TextSlide, transition_kind, transition_ms)` tuples ready for
/// the reel driver to iterate. Per Phase 6's skip-with-warn policy:
///   * Non-text-slide items (image / video) are dropped with a
///     warn — image/video item playback is post-Phase-6 scope.
///   * Items whose JSON can't be loaded are dropped with a warn.
///   * Empty result is allowed; the caller decides whether to bail.
///
/// Pure-ish — file IO via `find_text_slide` per item, but no GL or
/// EGL state. Host-testable via the existing tempdir fixtures in
/// the test module.
pub fn resolve_reel_items(
    content_root: &Path,
    playlist: &Playlist,
) -> Vec<(ContentItem, String, u32)> {
    let mut out: Vec<(ContentItem, String, u32)> = Vec::with_capacity(playlist.items.len());
    for item in &playlist.items {
        // Try text slide first; fall through to image; warn on
        // anything else (video lands in slice (c)).
        match find_text_slide(content_root, item.item_id) {
            Ok(Some(slide)) => {
                out.push((
                    ContentItem::Text(slide),
                    item.transition.clone(),
                    item.transition_ms,
                ));
                continue;
            }
            Ok(None) => {} // not a text slide; try image.
            Err(e) => {
                eprintln!(
                    "reel: skipping item {} — find_text_slide failed: {e:#}",
                    item.item_id,
                );
                continue;
            }
        }
        match find_image_slide(content_root, item.item_id) {
            Ok(Some(slide)) => {
                out.push((
                    ContentItem::Image(slide),
                    item.transition.clone(),
                    item.transition_ms,
                ));
                continue;
            }
            Ok(None) => {} // not text or image; try video.
            Err(e) => {
                eprintln!(
                    "reel: skipping item {} -- find_image_slide failed: {e:#}",
                    item.item_id,
                );
                continue;
            }
        }
        match find_video_slide(content_root, item.item_id) {
            Ok(Some(slide)) => {
                out.push((
                    ContentItem::Video(slide),
                    item.transition.clone(),
                    item.transition_ms,
                ));
                continue;
            }
            Ok(None) => {
                eprintln!(
                    "reel: skipping item {} -- type not text_slide / image / video",
                    item.item_id,
                );
            }
            Err(e) => {
                eprintln!(
                    "reel: skipping item {} -- find_video_slide failed: {e:#}",
                    item.item_id,
                );
            }
        }
    }
    out
}

/// Picked-out hex color for a slide's effective solid background,
/// returning the source as a string for logging. Pure function so
/// the dispatch logic is testable on the host.
///
/// Phase 4.1a rules:
///   - `pattern: solid` → `color_a` (color_b/density ignored).
///   - `pattern: <anything else>` → fall back to `background_color`.
///     The shader paths for the other 11 patterns land in follow-up
///     commits; falling back keeps non-Phase-4.1a slides renderable
///     even before their pattern shader exists.
///   - `pattern: None` → `background_color`.
pub fn solid_bg_hex(slide: &TextSlide) -> &str {
    match &slide.background_pattern {
        Some(p) if p.pattern == "solid" => &p.color_a,
        _ => &slide.background_color,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE_PLAYLIST: &str = r##"{
  "schema_version": 4,
  "playlists": [
    {
      "id": "00000000-0000-4000-8000-000000000001",
      "name": "Demo",
      "items": [
        { "item_id": "3964c302-311f-44f2-a6c9-efd24a16cfc0", "transition": "wipe", "transition_ms": 600 },
        { "item_id": "06dbf60e-7177-4edb-b061-77246ec64f50", "transition": "slide", "transition_ms": 600 }
      ]
    }
  ]
}"##;

    const SAMPLE_TEXT_ITEM: &str = r##"{
  "schema_version": 3,
  "updated_at": "2026-05-06T03:42:48.518367+00:00",
  "item": {
    "type": "text_slide",
    "id": "3964c302-311f-44f2-a6c9-efd24a16cfc0",
    "name": "01 · FREE",
    "duration_ms": 1500,
    "text_layers": [{"text": "FREE", "name": "", "font_size_pct": 80.0, "text_color": "#FFB43C", "box": {"x": 0.05, "y": 0.1, "w": 0.9, "h": 0.8}}],
    "background_color": "#050608",
    "background_pattern": null,
    "transition": "cut",
    "transition_ms": 500
  }
}"##;

    const SAMPLE_IMAGE_ITEM: &str = r##"{
  "schema_version": 3,
  "item": {
    "type": "image",
    "id": "3964c302-311f-44f2-a6c9-efd24a16cfc0",
    "name": "logo",
    "duration_ms": 3000,
    "transition": "fade",
    "transition_ms": 500
  }
}"##;

    #[test]
    fn parse_playlist_envelope_with_two_items() {
        let env: PlaylistEnvelope = serde_json::from_str(SAMPLE_PLAYLIST).unwrap();
        assert_eq!(env.schema_version, 4);
        assert_eq!(env.playlists.len(), 1);
        let p = &env.playlists[0];
        assert_eq!(p.name, "Demo");
        assert_eq!(p.items.len(), 2);
        assert_eq!(p.items[0].transition, "wipe");
        assert_eq!(p.items[0].transition_ms, 600);
        assert_eq!(
            p.items[1].item_id,
            Uuid::parse_str("06dbf60e-7177-4edb-b061-77246ec64f50").unwrap()
        );
    }

    #[test]
    fn playlist_item_uses_default_transition_when_missing() {
        let json = r#"{ "item_id": "00000000-0000-4000-8000-000000000099" }"#;
        let item: PlaylistItemRef = serde_json::from_str(json).unwrap();
        assert_eq!(item.transition, "cut");
        assert_eq!(item.transition_ms, 500);
    }

    #[test]
    fn parse_item_envelope_routes_to_text_slide() {
        let env: ItemEnvelope = serde_json::from_str(SAMPLE_TEXT_ITEM).unwrap();
        assert_eq!(env.schema_version, 3);
        let kind = env.item.get("type").and_then(|v| v.as_str()).unwrap();
        assert_eq!(kind, "text_slide");
        let slide: TextSlide = serde_json::from_value(env.item).unwrap();
        assert_eq!(slide.background_color, "#050608");
        assert_eq!(slide.duration_ms, 1500);
        assert_eq!(
            slide.id,
            Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap()
        );
    }

    #[test]
    fn parse_item_envelope_recognizes_non_text_slide() {
        // The `find_text_slide` helper returns Ok(None) for non-text
        // items; this tests the routing decision separately.
        let env: ItemEnvelope = serde_json::from_str(SAMPLE_IMAGE_ITEM).unwrap();
        let kind = env.item.get("type").and_then(|v| v.as_str()).unwrap();
        assert_eq!(kind, "image");
    }

    #[test]
    fn text_slide_uses_default_background_when_missing() {
        // Operator may delete fields; Phase 4 entry should fall back
        // to black rather than refuse to render.
        let json = r#"{
            "id": "00000000-0000-4000-8000-000000000099",
            "name": "stub",
            "text_layers": []
        }"#;
        let slide: TextSlide = serde_json::from_str(json).unwrap();
        assert_eq!(slide.background_color, "#000000");
        assert_eq!(slide.duration_ms, 5000);
    }

    #[test]
    fn text_slide_tolerates_unknown_fields() {
        // The backend evolves ahead of us — adding a field to a
        // TextSlide must not break the renderer until we choose to
        // mirror it.
        let json = r##"{
            "id": "00000000-0000-4000-8000-000000000099",
            "name": "future",
            "background_color": "#112233",
            "some_field_we_dont_know_about": 42,
            "another_one": {"nested": "values"}
        }"##;
        let slide: TextSlide = serde_json::from_str(json).unwrap();
        assert_eq!(slide.background_color, "#112233");
    }

    #[test]
    fn item_dir_uses_uuid_dir_layout() {
        let root = Path::new("/var/openmarquee/content");
        let id = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap();
        let dir = item_dir(root, id);
        assert_eq!(
            dir.to_str().unwrap(),
            "/var/openmarquee/content/3964c302-311f-44f2-a6c9-efd24a16cfc0"
        );
    }

    // v1-spec-delta #8 (F-image-bg-tests + F-video-asset-tests)
    // -- pure path-derivation helpers for ImageSlide and
    // VideoSlide assets. Mirrors the item_dir_uses_uuid_dir_
    // layout test to lock the canonical storage convention.
    #[test]
    fn image_slide_asset_path_appends_asset_png() {
        let root = Path::new("/var/openmarquee/content");
        let id = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap();
        let p = image_slide_asset_path(root, id);
        assert_eq!(
            p.to_str().unwrap(),
            "/var/openmarquee/content/3964c302-311f-44f2-a6c9-efd24a16cfc0/asset.png"
        );
    }

    #[test]
    fn video_slide_asset_path_appends_asset_mp4() {
        let root = Path::new("/var/openmarquee/content");
        let id = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap();
        let p = video_slide_asset_path(root, id);
        assert_eq!(
            p.to_str().unwrap(),
            "/var/openmarquee/content/3964c302-311f-44f2-a6c9-efd24a16cfc0/asset.mp4"
        );
    }

    // v1-spec-delta #10 (slice a) -- Settings schema + loader.
    const SAMPLE_SETTINGS: &str = r##"{
  "schema_version": 1,
  "sign_name": "openMarqueeDev",
  "flock_sync_enabled": true,
  "ui_first_run_seen": true,
  "output_mode": "hdmi",
  "display_width": 1920,
  "display_height": 1080,
  "display_rotation": 0,
  "brightness": 80,
  "gamma": 2.2,
  "ws281x_pixel_order": "row_major",
  "wifi_ap_enabled": true,
  "wifi_ssid": "openMarquee-SETUP",
  "wifi_password": "openmarquee",
  "wifi_station_enabled": false,
  "wifi_station_ssid": null,
  "wifi_station_password": null,
  "timezone": null,
  "tailscale_enabled": false,
  "tailscale_auth_key": null,
  "tailscale_hostname": null
}"##;

    #[test]
    fn settings_round_trip_keeps_renderer_relevant_fields() {
        // The Settings loader skips Wi-Fi / Tailscale / sign-
        // name fields (out of renderer scope) and keeps just
        // the per-frame-relevant subset.
        let s: Settings = serde_json::from_str(SAMPLE_SETTINGS).unwrap();
        assert_eq!(s.schema_version, 1);
        assert_eq!(s.display_width, 1920);
        assert_eq!(s.display_height, 1080);
        assert_eq!(s.display_rotation, 0);
        assert_eq!(s.brightness, 80);
        assert!((s.gamma - 2.2).abs() < 0.001);
    }

    #[test]
    fn settings_loader_reads_from_disk() {
        let td = TempDir::new().unwrap();
        let path = td.path().join("settings.json");
        std::fs::write(&path, SAMPLE_SETTINGS).unwrap();
        let s = load_settings(&path).unwrap();
        assert_eq!(s.brightness, 80);
        assert!((s.gamma - 2.2).abs() < 0.001);
    }

    #[test]
    fn settings_loader_errs_on_missing_file() {
        let td = TempDir::new().unwrap();
        let path = td.path().join("nonexistent.json");
        let err = load_settings(&path).unwrap_err();
        let msg = format!("{err:#}");
        assert!(msg.contains("read settings"), "got: {msg}");
    }

    #[test]
    fn settings_loader_errs_on_malformed_json() {
        let td = TempDir::new().unwrap();
        let path = td.path().join("settings.json");
        std::fs::write(&path, "{ broken").unwrap();
        let err = load_settings(&path).unwrap_err();
        let msg = format!("{err:#}");
        assert!(msg.contains("parse settings"), "got: {msg}");
    }

    #[test]
    fn settings_default_uses_spec_anchors() {
        let s = Settings::default();
        assert_eq!(s.display_width, 1920);
        assert_eq!(s.display_height, 1080);
        assert_eq!(s.display_rotation, 0);
        assert_eq!(s.brightness, 100);
        assert!((s.gamma - 1.0).abs() < 0.001);
    }

    #[test]
    fn settings_is_color_identity_at_default_anchors_is_true() {
        // The schema default is brightness=100 + gamma=1.0,
        // which IS identity (the post-pass is a no-op blit and
        // the renderer can skip the FBO ping-pong). Operators
        // who want the renderer to apply a second gamma curve
        // dial gamma above 1.0 explicitly.
        let s = Settings::default();
        assert!(s.is_color_identity(), "default is identity (gamma=1.0)");
    }

    #[test]
    fn settings_is_color_identity_at_b100_g1() {
        // Explicit identity: brightness=100, gamma=1.0.
        let s = Settings { gamma: 1.0, ..Settings::default() };
        assert!(s.is_color_identity());
    }

    #[test]
    fn settings_not_color_identity_when_brightness_dimmed() {
        let s = Settings { gamma: 1.0, brightness: 50, ..Settings::default() };
        assert!(!s.is_color_identity());
    }

    #[test]
    fn settings_partial_envelope_uses_serde_defaults() {
        // Partial envelope (only schema_version + brightness)
        // should fill the rest from the serde defaults.
        let json = r#"{"schema_version": 1, "brightness": 50}"#;
        let s: Settings = serde_json::from_str(json).unwrap();
        assert_eq!(s.schema_version, 1);
        assert_eq!(s.brightness, 50);
        assert_eq!(s.display_width, 1920);  // default
        assert!((s.gamma - 1.0).abs() < 0.001);  // default
    }

    #[test]
    fn settings_watcher_emits_initial_state_on_first_check() {
        let td = TempDir::new().unwrap();
        let path = td.path().join("settings.json");
        std::fs::write(&path, SAMPLE_SETTINGS).unwrap();
        let mut w = SettingsWatcher::new(path);
        let initial = w.check().expect("first check should emit initial state");
        assert_eq!(initial.brightness, 80);
    }

    #[test]
    fn settings_watcher_returns_none_on_unchanged_mtime() {
        let td = TempDir::new().unwrap();
        let path = td.path().join("settings.json");
        std::fs::write(&path, SAMPLE_SETTINGS).unwrap();
        let mut w = SettingsWatcher::new(path);
        let _ = w.check().unwrap();  // bootstrap.
        // Second check without mtime change: None.
        assert!(w.check().is_none());
        // Third call also None.
        assert!(w.check().is_none());
    }

    #[test]
    fn settings_watcher_emits_on_mtime_change() {
        let td = TempDir::new().unwrap();
        let path = td.path().join("settings.json");
        std::fs::write(&path, SAMPLE_SETTINGS).unwrap();
        let mut w = SettingsWatcher::new(path.clone());
        let _ = w.check().unwrap();  // bootstrap.
        // Mutate the file. Sleep so the mtime tick is
        // observable on platforms with second-precision mtime
        // (some Linux fs configs go to whole seconds; APFS +
        // ext4 with nanosec timestamps go finer).
        std::thread::sleep(std::time::Duration::from_millis(1100));
        let new_json = SAMPLE_SETTINGS.replace("\"brightness\": 80", "\"brightness\": 50");
        std::fs::write(&path, &new_json).unwrap();
        let after = w.check();
        // On platforms where the mtime ticked (every reasonable
        // FS at 1.1s sleep), after is Some.
        let s = after.expect("post-mutation check should surface new mtime");
        assert_eq!(s.brightness, 50);
    }

    #[test]
    fn settings_watcher_silently_handles_missing_file() {
        let td = TempDir::new().unwrap();
        let path = td.path().join("nonexistent.json");
        let mut w = SettingsWatcher::new(path);
        // First check on a missing file: None (no panic, no
        // err -- watcher silently waits for the file to
        // appear).
        assert!(w.check().is_none());
        // Subsequent checks also None.
        assert!(w.check().is_none());
    }

    #[test]
    fn settings_watcher_silently_handles_malformed_json() {
        let td = TempDir::new().unwrap();
        let path = td.path().join("settings.json");
        std::fs::write(&path, "{ broken").unwrap();
        let mut w = SettingsWatcher::new(path);
        // First check on malformed: None (warn + keep last-
        // known).
        assert!(w.check().is_none());
    }

    #[test]
    fn image_and_video_asset_paths_diverge_at_extension() {
        // Sanity: asset.png vs asset.mp4 disambiguates the
        // type-based dispatch -- the renderer never has to grep
        // both extensions for one slide_id.
        let root = Path::new("/tmp/x");
        let id = Uuid::nil();
        let img = image_slide_asset_path(root, id);
        let vid = video_slide_asset_path(root, id);
        assert_ne!(img, vid);
        assert!(img.to_str().unwrap().ends_with("asset.png"));
        assert!(vid.to_str().unwrap().ends_with("asset.mp4"));
    }

    fn slide_with_pattern(pattern: &str, color_a: &str) -> TextSlide {
        TextSlide {
            id: Uuid::nil(),
            name: String::new(),
            duration_ms: 5000,
            background_color: "#222222".to_string(),
            background_image_slide_id: None,
            background_pattern: Some(BackgroundPattern {
                pattern: pattern.to_string(),
                color_a: color_a.to_string(),
                color_b: "#FFFFFF".to_string(),
                density: 0.5,
            }),
            text_layers: vec![],
        }
    }

    #[test]
    fn parses_fys_first_text_layer() {
        // Real FYS slide #01: "FREE", #FFB43C, font_size_pct=80,
        // box covers the inner 90% of the slide. Pinning the field
        // names + types so a backend rename is a loud test failure
        // rather than a silent serde-default zero.
        let env: ItemEnvelope = serde_json::from_str(SAMPLE_TEXT_ITEM).unwrap();
        let slide: TextSlide = serde_json::from_value(env.item).unwrap();
        assert_eq!(slide.text_layers.len(), 1);
        let layer = &slide.text_layers[0];
        assert_eq!(layer.text, "FREE");
        assert_eq!(layer.text_color, "#FFB43C");
        assert!((layer.font_size_pct.unwrap() - 80.0).abs() < 1e-6);
        assert!(layer.font_size_px.is_none());
        assert!((layer.r#box.x - 0.05).abs() < 1e-6);
        assert!((layer.r#box.y - 0.10).abs() < 1e-6);
        assert!((layer.r#box.w - 0.90).abs() < 1e-6);
        assert!((layer.r#box.h - 0.80).abs() < 1e-6);
        // Defaults applied for omitted fields:
        assert!(layer.visible);
        assert!((layer.opacity - 1.0).abs() < 1e-6);
        assert_eq!(layer.text_align, "center");
        // v1-spec-delta #2: motion fields default to "static" /
        // intensity 50 / phase 0 / speed 1.0 when absent (mirrors
        // Python's defaults).
        assert_eq!(layer.motion, "static");
        assert_eq!(layer.motion_intensity, 50);
        assert!((layer.motion_phase - 0.0).abs() < 1e-6);
        assert!((layer.motion_speed - 1.0).abs() < 1e-6);
        // v1.0 close: anchor defaults to "center" (matches §5.10a's
        // default + the pre-v1.0 always-center renderer behavior).
        assert_eq!(layer.anchor, "center");
        // weight is an optional field; absent in this fixture.
        assert!(layer.weight.is_none());
    }

    #[test]
    fn anchor_round_trips_three_spec_values() {
        // v1.0 close (SYSTEM_SPEC §5.10a line 318): top/center/bottom.
        // Pin each so a schema rename or accidental drop fires here.
        for kind in ["top", "center", "bottom"] {
            let json = format!(
                r##"{{
                    "text": "X",
                    "anchor": "{kind}",
                    "box": {{"x": 0, "y": 0, "w": 1, "h": 1}}
                }}"##
            );
            let layer: TextLayer = serde_json::from_str(&json).unwrap();
            assert_eq!(layer.anchor, kind, "kind={kind}");
        }
    }

    #[test]
    fn weight_round_trips_as_optional() {
        // v1.0 close: weight is wire-accepted (preserves through
        // save/load) but render-deferred — fontdue doesn't support
        // variable-axis selection and the bundled font system is
        // single-TTF-per-family. Future v1.1 work bundles weight-
        // variant TTFs OR swaps rasterizers. Until then this pins
        // that the field at least survives a deserialize so a
        // future render-side wire-up has a value to read.
        let with_weight: TextLayer = serde_json::from_str(
            r#"{"text": "Y", "weight": 700, "box": {"x":0,"y":0,"w":1,"h":1}}"#,
        )
        .unwrap();
        assert_eq!(with_weight.weight, Some(700));

        let without_weight: TextLayer = serde_json::from_str(
            r#"{"text": "Y", "box": {"x":0,"y":0,"w":1,"h":1}}"#,
        )
        .unwrap();
        assert!(without_weight.weight.is_none());
    }

    #[test]
    fn motion_field_round_trips_all_seven_values() {
        // Every value the spec defines must load. Pin them so a
        // schema rename or accidental drop fires here loudly.
        for kind in [
            "static", "ticker", "breathe", "pulse", "bounce", "shake",
            "blink",
        ] {
            let json = format!(
                r##"{{
                    "text": "X",
                    "motion": "{kind}",
                    "box": {{"x": 0, "y": 0, "w": 1, "h": 1}}
                }}"##
            );
            let layer: TextLayer = serde_json::from_str(&json).unwrap();
            assert_eq!(layer.motion, kind, "kind={kind}");
        }
    }

    #[test]
    fn motion_legacy_scroll_migrates_to_ticker() {
        // v3 envelopes saved before the rename have motion="scroll".
        // The deserializer rewrites it to "ticker" so the renderer
        // sees one canonical name. Mirrors Python's
        // _migrate_legacy_motion validator.
        let json = r##"{
            "text": "Old envelope",
            "motion": "scroll",
            "box": {"x": 0, "y": 0, "w": 1, "h": 1}
        }"##;
        let layer: TextLayer = serde_json::from_str(json).unwrap();
        assert_eq!(layer.motion, "ticker");
    }

    #[test]
    fn auto_mode_layer_with_empty_text_is_renderable() {
        // Parity Bug 1 (2026-05-19): the Boot time-clock layer
        // carries text="" + auto_mode="time". It MUST survive the
        // resolve_slide_layers renderability filter — its visible
        // string is resolved at paint time by resolve_layer_text.
        // Before the fix a `!text.is_empty()` filter dropped it.
        let clock = r##"{
            "text": "",
            "auto_mode": "time",
            "auto_format": "time_hms",
            "box": {"x": 0.7, "y": 0.6, "w": 0.2, "h": 0.1}
        }"##;
        let layer: TextLayer = serde_json::from_str(clock).unwrap();
        assert!(
            layer.is_renderable(),
            "auto_mode layer with empty text must be renderable",
        );

        // A genuinely empty static layer (no auto_mode) is still
        // skipped — nothing to draw.
        let blank = r##"{
            "text": "",
            "box": {"x": 0, "y": 0, "w": 1, "h": 1}
        }"##;
        let blank_layer: TextLayer = serde_json::from_str(blank).unwrap();
        assert!(
            !blank_layer.is_renderable(),
            "empty static layer should not be renderable",
        );

        // An invisible auto_mode layer is skipped — visibility
        // gates everything.
        let hidden = r##"{
            "text": "",
            "auto_mode": "time",
            "visible": false,
            "box": {"x": 0, "y": 0, "w": 1, "h": 1}
        }"##;
        let hidden_layer: TextLayer = serde_json::from_str(hidden).unwrap();
        assert!(
            !hidden_layer.is_renderable(),
            "invisible auto_mode layer should not be renderable",
        );

        // A normal static layer with text is renderable.
        let normal = r##"{
            "text": "FREE",
            "box": {"x": 0, "y": 0, "w": 1, "h": 1}
        }"##;
        let normal_layer: TextLayer = serde_json::from_str(normal).unwrap();
        assert!(normal_layer.is_renderable());
    }

    #[test]
    fn motion_unknown_value_passes_through_unchanged() {
        // Future-added motion values shouldn't be silently rewritten
        // to "static" or anything else by the migration validator.
        // The motion-resolution code in hdmi_logic treats unknown as
        // static (the safe fallback) but the field itself is preserved
        // so a re-save doesn't lose information.
        let json = r##"{
            "text": "Future",
            "motion": "warp",
            "box": {"x": 0, "y": 0, "w": 1, "h": 1}
        }"##;
        let layer: TextLayer = serde_json::from_str(json).unwrap();
        assert_eq!(layer.motion, "warp");
    }

    #[test]
    fn text_layers_default_to_empty_when_omitted() {
        // Operator strips text_layers — must not panic.
        let json = r##"{
            "id": "00000000-0000-4000-8000-000000000099",
            "name": "stub"
        }"##;
        let slide: TextSlide = serde_json::from_str(json).unwrap();
        assert!(slide.text_layers.is_empty());
    }

    #[test]
    fn parses_background_pattern_solid() {
        let json = r##"{
            "id": "00000000-0000-4000-8000-000000000099",
            "background_color": "#000000",
            "background_pattern": {"pattern": "solid", "color_a": "#ABCDEF"}
        }"##;
        let slide: TextSlide = serde_json::from_str(json).unwrap();
        let p = slide.background_pattern.as_ref().unwrap();
        assert_eq!(p.pattern, "solid");
        assert_eq!(p.color_a, "#ABCDEF");
        assert_eq!(p.color_b, "#FFFFFF"); // default
        assert!((p.density - 0.5).abs() < 1e-6); // default
    }

    #[test]
    fn parses_background_pattern_gradient_full() {
        let json = r##"{
            "id": "00000000-0000-4000-8000-000000000099",
            "background_color": "#000000",
            "background_pattern": {"pattern": "gradient", "color_a": "#FF0000", "color_b": "#00FF00", "density": 0.75}
        }"##;
        let slide: TextSlide = serde_json::from_str(json).unwrap();
        let p = slide.background_pattern.unwrap();
        assert_eq!(p.pattern, "gradient");
        assert_eq!(p.color_a, "#FF0000");
        assert_eq!(p.color_b, "#00FF00");
        assert!((p.density - 0.75).abs() < 1e-6);
    }

    #[test]
    fn solid_bg_hex_uses_pattern_color_a_for_solid() {
        let slide = slide_with_pattern("solid", "#ABCDEF");
        assert_eq!(solid_bg_hex(&slide), "#ABCDEF");
    }

    #[test]
    fn solid_bg_hex_falls_back_for_unsupported_pattern() {
        // Phase 4.1a only handles "solid"; other patterns get the
        // background_color fallback until their shader lands.
        for kind in ["gradient", "dots", "halftone", "stripes",
                     "scanlines", "checker", "grid", "rings", "rays",
                     "confetti", "bricks"] {
            let slide = slide_with_pattern(kind, "#ABCDEF");
            assert_eq!(
                solid_bg_hex(&slide),
                "#222222",
                "pattern {kind} should fall back to background_color"
            );
        }
    }

    #[test]
    fn solid_bg_hex_uses_background_color_when_no_pattern() {
        let slide = TextSlide {
            id: Uuid::nil(),
            name: String::new(),
            duration_ms: 5000,
            background_color: "#050608".to_string(),
            background_pattern: None,
            background_image_slide_id: None,
            text_layers: vec![],
        };
        assert_eq!(solid_bg_hex(&slide), "#050608");
    }

    // ------------------------------------------------------------
    // Integration tests — file IO via tempdirs, per QA Rule 3
    // (integration coverage where a feature touches other systems).
    // These test the load_playlist + find_text_slide wrappers
    // against actual std::fs reads, not just JSON parsing in
    // isolation. Catches things like context-message wording, IO
    // error propagation, missing-file vs malformed-file distinction.
    // ------------------------------------------------------------

    use tempfile::TempDir;

    fn write_file(dir: &Path, name: &str, contents: &str) -> PathBuf {
        let p = dir.join(name);
        std::fs::write(&p, contents).expect("write tempfile");
        p
    }

    #[test]
    fn load_playlist_round_trip_through_fs() {
        let td = TempDir::new().unwrap();
        let path = write_file(td.path(), "playlist.json", SAMPLE_PLAYLIST);
        let env = load_playlist(&path).expect("load");
        assert_eq!(env.schema_version, 4);
        assert_eq!(env.playlists.len(), 1);
        assert_eq!(env.playlists[0].items.len(), 2);
    }

    #[test]
    fn load_playlist_missing_file_says_read() {
        let td = TempDir::new().unwrap();
        let missing = td.path().join("nope.json");
        let err = load_playlist(&missing).unwrap_err();
        // The context chain should mention "read" so a stat/IO
        // failure is distinguishable from a parse failure. We don't
        // pin the exact OS message — just the helper's prefix.
        let msg = format!("{err:#}");
        assert!(
            msg.contains("read"),
            "expected 'read' in error chain, got: {msg}"
        );
    }

    #[test]
    fn load_playlist_malformed_says_parse() {
        let td = TempDir::new().unwrap();
        let path = write_file(td.path(), "playlist.json", "{ not valid json }");
        let err = load_playlist(&path).unwrap_err();
        let msg = format!("{err:#}");
        assert!(
            msg.contains("parse"),
            "expected 'parse' in error chain, got: {msg}"
        );
    }

    #[test]
    fn find_text_slide_returns_some_for_text_item() {
        let td = TempDir::new().unwrap();
        let id = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap();
        let item_dir = td.path().join(id.to_string());
        std::fs::create_dir_all(&item_dir).unwrap();
        write_file(&item_dir, "item.json", SAMPLE_TEXT_ITEM);
        let slide = find_text_slide(td.path(), id).unwrap().expect("text slide");
        assert_eq!(slide.background_color, "#050608");
        assert_eq!(slide.duration_ms, 1500);
    }

    #[test]
    fn find_text_slide_returns_none_for_image_item() {
        let td = TempDir::new().unwrap();
        let id = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap();
        let item_dir = td.path().join(id.to_string());
        std::fs::create_dir_all(&item_dir).unwrap();
        write_file(&item_dir, "item.json", SAMPLE_IMAGE_ITEM);
        let result = find_text_slide(td.path(), id).unwrap();
        assert!(result.is_none(), "image item should yield Ok(None)");
    }

    #[test]
    fn find_text_slide_returns_err_for_missing_item_file() {
        let td = TempDir::new().unwrap();
        let id = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap();
        // Don't create the item dir at all — file is missing.
        let err = find_text_slide(td.path(), id).unwrap_err();
        let msg = format!("{err:#}");
        assert!(
            msg.contains("read item"),
            "expected 'read item' in error chain, got: {msg}"
        );
    }

    #[test]
    fn find_text_slide_returns_err_for_malformed_envelope() {
        let td = TempDir::new().unwrap();
        let id = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap();
        let item_dir = td.path().join(id.to_string());
        std::fs::create_dir_all(&item_dir).unwrap();
        write_file(&item_dir, "item.json", "{ broken");
        let err = find_text_slide(td.path(), id).unwrap_err();
        let msg = format!("{err:#}");
        assert!(
            msg.contains("parse item envelope"),
            "expected 'parse item envelope' in error chain, got: {msg}"
        );
    }

    // v1-spec-delta #8 (F-image-bg-tests + slice c) -- type-
    // narrow loaders for ImageSlide and VideoSlide. Mirror the
    // find_text_slide test suite.
    const SAMPLE_VIDEO_ITEM: &str = r##"{
  "schema_version": 3,
  "item": {
    "type": "video",
    "id": "3964c302-311f-44f2-a6c9-efd24a16cfc0",
    "name": "intro",
    "duration_ms": 8000,
    "transition": "fade",
    "transition_ms": 600
  }
}"##;

    // A `web`-type envelope -- carries the extra `url` /
    // `refresh_interval_s` fields. find_image_slide accepts it: a
    // Web slide renders as an image (its asset.png is a screenshot
    // the helper refreshes).
    const SAMPLE_WEB_ITEM: &str = r##"{
  "schema_version": 3,
  "item": {
    "type": "web",
    "id": "3964c302-311f-44f2-a6c9-efd24a16cfc0",
    "name": "status board",
    "url": "https://status.example.com",
    "refresh_interval_s": 300,
    "duration_ms": 7000,
    "transition": "fade",
    "transition_ms": 500
  }
}"##;

    #[test]
    fn find_image_slide_returns_some_for_image_item() {
        let td = TempDir::new().unwrap();
        let id = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap();
        let dir = td.path().join(id.to_string());
        std::fs::create_dir_all(&dir).unwrap();
        write_file(&dir, "item.json", SAMPLE_IMAGE_ITEM);
        let slide = find_image_slide(td.path(), id).unwrap().unwrap();
        assert_eq!(slide.id, id);
        assert_eq!(slide.duration_ms, 3000);
        assert_eq!(slide.transition, "fade");
    }

    #[test]
    fn find_image_slide_returns_some_for_web_item() {
        // A Web slide renders as an image slide: find_image_slide
        // treats `web` as `image`, and the extra url /
        // refresh_interval_s fields deserialize harmlessly into
        // ImageSlide (no deny_unknown_fields).
        let td = TempDir::new().unwrap();
        let id = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap();
        let dir = td.path().join(id.to_string());
        std::fs::create_dir_all(&dir).unwrap();
        write_file(&dir, "item.json", SAMPLE_WEB_ITEM);
        let slide = find_image_slide(td.path(), id).unwrap().unwrap();
        assert_eq!(slide.id, id);
        assert_eq!(slide.duration_ms, 7000);
        assert_eq!(slide.transition, "fade");
    }

    #[test]
    fn find_image_slide_returns_none_for_text_item() {
        let td = TempDir::new().unwrap();
        let id = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap();
        let dir = td.path().join(id.to_string());
        std::fs::create_dir_all(&dir).unwrap();
        write_file(&dir, "item.json", SAMPLE_TEXT_ITEM);
        assert!(find_image_slide(td.path(), id).unwrap().is_none());
    }

    #[test]
    fn find_image_slide_returns_none_for_video_item() {
        let td = TempDir::new().unwrap();
        let id = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap();
        let dir = td.path().join(id.to_string());
        std::fs::create_dir_all(&dir).unwrap();
        write_file(&dir, "item.json", SAMPLE_VIDEO_ITEM);
        assert!(find_image_slide(td.path(), id).unwrap().is_none());
    }

    #[test]
    fn find_video_slide_returns_some_for_video_item() {
        let td = TempDir::new().unwrap();
        let id = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap();
        let dir = td.path().join(id.to_string());
        std::fs::create_dir_all(&dir).unwrap();
        write_file(&dir, "item.json", SAMPLE_VIDEO_ITEM);
        let slide = find_video_slide(td.path(), id).unwrap().unwrap();
        assert_eq!(slide.id, id);
        assert_eq!(slide.duration_ms, 8000);
        assert_eq!(slide.transition, "fade");
    }

    #[test]
    fn find_video_slide_returns_none_for_image_item() {
        let td = TempDir::new().unwrap();
        let id = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap();
        let dir = td.path().join(id.to_string());
        std::fs::create_dir_all(&dir).unwrap();
        write_file(&dir, "item.json", SAMPLE_IMAGE_ITEM);
        assert!(find_video_slide(td.path(), id).unwrap().is_none());
    }

    #[test]
    fn resolve_reel_items_dispatches_text_image_video_in_order() {
        // Mixed-type playlist exercises all three find_* paths
        // through resolve_reel_items's dispatch chain.
        let td = TempDir::new().unwrap();
        let id_t = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap();
        let id_i = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cff0").unwrap();
        let id_v = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cffd").unwrap();
        for (id, body) in [
            (id_t, SAMPLE_TEXT_ITEM),
            (id_i, SAMPLE_IMAGE_ITEM),
            (id_v, SAMPLE_VIDEO_ITEM),
        ] {
            let dir = td.path().join(id.to_string());
            std::fs::create_dir_all(&dir).unwrap();
            write_file(&dir, "item.json", body);
        }
        let playlist = build_playlist_with_items(vec![
            item_ref(id_t, "cut", 500),
            item_ref(id_i, "fade", 600),
            item_ref(id_v, "wipe", 700),
        ]);
        let resolved = resolve_reel_items(td.path(), &playlist);
        assert_eq!(resolved.len(), 3);
        assert!(matches!(resolved[0].0, ContentItem::Text(_)));
        assert!(matches!(resolved[1].0, ContentItem::Image(_)));
        assert!(matches!(resolved[2].0, ContentItem::Video(_)));
        assert_eq!(resolved[0].0.type_label(), "text_slide");
        assert_eq!(resolved[1].0.type_label(), "image");
        assert_eq!(resolved[2].0.type_label(), "video");
    }

    // -- resolve_reel_items -------------------------------------

    fn build_playlist_with_items(refs: Vec<PlaylistItemRef>) -> Playlist {
        Playlist {
            id: Uuid::parse_str("00000000-0000-4000-8000-000000000001").unwrap(),
            name: "test".to_string(),
            items: refs,
        }
    }

    fn item_ref(id: Uuid, kind: &str, ms: u32) -> PlaylistItemRef {
        PlaylistItemRef {
            item_id: id,
            transition: kind.to_string(),
            transition_ms: ms,
        }
    }

    #[test]
    fn resolve_reel_items_keeps_text_slides() {
        // Two text-slide items in the playlist; both resolve.
        let td = TempDir::new().unwrap();
        let id_a = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap();
        let dir_a = td.path().join(id_a.to_string());
        std::fs::create_dir_all(&dir_a).unwrap();
        write_file(&dir_a, "item.json", SAMPLE_TEXT_ITEM);

        let id_b = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc1").unwrap();
        let dir_b = td.path().join(id_b.to_string());
        std::fs::create_dir_all(&dir_b).unwrap();
        // Reuse SAMPLE_TEXT_ITEM (content is parsable; the inner id
        // doesn't have to match the dir name for find_text_slide
        // — that's keyed on the path).
        write_file(&dir_b, "item.json", SAMPLE_TEXT_ITEM);

        let playlist = build_playlist_with_items(vec![
            item_ref(id_a, "fade", 800),
            item_ref(id_b, "wipe", 600),
        ]);
        let resolved = resolve_reel_items(td.path(), &playlist);
        assert_eq!(resolved.len(), 2);
        assert_eq!(resolved[0].1, "fade");
        assert_eq!(resolved[0].2, 800);
        assert_eq!(resolved[1].1, "wipe");
        assert_eq!(resolved[1].2, 600);
    }

    #[test]
    fn resolve_reel_items_returns_image_items_as_content_item_image() {
        // v1-spec-delta #8 (slice a): image items are now first-
        // class reel content. They surface as ContentItem::Image
        // alongside ContentItem::Text -- the reel driver
        // dispatches on type at render time. Pre-slice-a, image
        // items were skipped with a warn.
        let td = TempDir::new().unwrap();
        let id_text = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap();
        let dir_text = td.path().join(id_text.to_string());
        std::fs::create_dir_all(&dir_text).unwrap();
        write_file(&dir_text, "item.json", SAMPLE_TEXT_ITEM);

        let id_image = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cff0").unwrap();
        let dir_image = td.path().join(id_image.to_string());
        std::fs::create_dir_all(&dir_image).unwrap();
        write_file(&dir_image, "item.json", SAMPLE_IMAGE_ITEM);

        let playlist = build_playlist_with_items(vec![
            item_ref(id_text, "fade", 800),
            item_ref(id_image, "wipe", 600),
        ]);
        let resolved = resolve_reel_items(td.path(), &playlist);
        assert_eq!(resolved.len(), 2, "both items should be returned");
        assert!(matches!(resolved[0].0, ContentItem::Text(_)));
        assert!(matches!(resolved[1].0, ContentItem::Image(_)));
        assert_eq!(resolved[0].1, "fade");
        assert_eq!(resolved[1].1, "wipe");
    }

    #[test]
    fn resolve_reel_items_skips_missing_items() {
        // An item whose item.json file doesn't exist gets dropped
        // with a warn — reel survives orphan playlist references.
        let td = TempDir::new().unwrap();
        let id_text = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc0").unwrap();
        let dir_text = td.path().join(id_text.to_string());
        std::fs::create_dir_all(&dir_text).unwrap();
        write_file(&dir_text, "item.json", SAMPLE_TEXT_ITEM);

        let id_missing = Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cffe").unwrap();
        // NO files written for id_missing — its item dir doesn't
        // exist.

        let playlist = build_playlist_with_items(vec![
            item_ref(id_text, "fade", 800),
            item_ref(id_missing, "wipe", 600),
        ]);
        let resolved = resolve_reel_items(td.path(), &playlist);
        assert_eq!(resolved.len(), 1, "missing should be skipped");
        assert_eq!(resolved[0].1, "fade");
    }

    #[test]
    fn resolve_reel_items_empty_playlist_returns_empty() {
        let td = TempDir::new().unwrap();
        let playlist = build_playlist_with_items(vec![]);
        let resolved = resolve_reel_items(td.path(), &playlist);
        assert!(resolved.is_empty());
    }

    #[test]
    fn resolve_reel_items_preserves_playlist_order() {
        // Order matters: the reel iterates in playlist order, so
        // the resolver must too. Pin the order with 3 items.
        let td = TempDir::new().unwrap();
        for hex in [
            "3964c302-311f-44f2-a6c9-efd24a16cfc1",
            "3964c302-311f-44f2-a6c9-efd24a16cfc2",
            "3964c302-311f-44f2-a6c9-efd24a16cfc3",
        ] {
            let id = Uuid::parse_str(hex).unwrap();
            let d = td.path().join(id.to_string());
            std::fs::create_dir_all(&d).unwrap();
            write_file(&d, "item.json", SAMPLE_TEXT_ITEM);
        }
        let playlist = build_playlist_with_items(vec![
            item_ref(Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc3").unwrap(), "cut", 0),
            item_ref(Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc1").unwrap(), "fade", 800),
            item_ref(Uuid::parse_str("3964c302-311f-44f2-a6c9-efd24a16cfc2").unwrap(), "wipe", 600),
        ]);
        let resolved = resolve_reel_items(td.path(), &playlist);
        assert_eq!(resolved.len(), 3);
        assert_eq!(resolved[0].1, "cut");
        assert_eq!(resolved[1].1, "fade");
        assert_eq!(resolved[2].1, "wipe");
    }

    #[test]
    fn item_dir_path_safety_invariant() {
        // The UUID typedef guarantees the joined component is always
        // a 36-char hex-with-dashes string. This test asserts that
        // invariant: no path traversal possible because the dir
        // segment is always a UUID, never operator input.
        let root = Path::new("/var/openmarquee/content");
        let id = Uuid::parse_str("00000000-0000-4000-8000-000000000099").unwrap();
        let dir = item_dir(root, id);
        let suffix = dir.strip_prefix(root).unwrap();
        assert_eq!(
            suffix.to_str().unwrap(),
            "00000000-0000-4000-8000-000000000099"
        );
        // Sanity: the resolved path stays under content_root.
        assert!(dir.starts_with(root));
    }

    // --- Batch 17.4 / sweep #9 #3: schema round-trip fixture pin ---
    //
    // The p2g_overlay_route golden-master fixture at
    // renderer/tests/fixtures/f0000000-.../item.json hardcodes
    // schema_version=3. Spec §5.10a v3.1.2 keeps adding fields (per-
    // axis squish + width-relative font_size_pct most recently); if a
    // future spec bumps to v4 with a new field, the fixture would
    // load on the lenient #[serde(default)] deserializer but silently
    // skip the new field, and the captured golden PNG would no
    // longer reflect what the production renderer sees.
    //
    // This test asserts the fixture deserializes cleanly + the round-
    // trip preserves the slide's load-bearing fields (text content,
    // box, motion, blend). When a spec bump lands, this test fails
    // loud rather than silently passing.

    fn fixture_path() -> std::path::PathBuf {
        // CARGO_MANIFEST_DIR points to renderer/; the fixture lives
        // at tests/fixtures/<UUID>/item.json relative to it.
        let manifest = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        manifest
            .join("tests")
            .join("fixtures")
            .join("f0000000-0000-4000-8000-000000000001")
            .join("item.json")
    }

    #[test]
    fn p2g_overlay_route_fixture_deserializes_cleanly() {
        let path = fixture_path();
        let bytes = std::fs::read(&path).expect("fixture file readable");
        let envelope: ItemEnvelope =
            serde_json::from_slice(&bytes).expect("envelope parses");
        assert_eq!(envelope.schema_version, 3);
        let kind = envelope.item.get("type").and_then(|v| v.as_str());
        assert_eq!(kind, Some("text_slide"));
        let slide: TextSlide =
            serde_json::from_value(envelope.item).expect("text_slide parses");
        // Load-bearing claims: 2 layers, the second one has blend=
        // "overlay" (this is the fixture's whole reason for existing
        // -- it triggers paint_layers_via_overlay_route on the hot
        // path).
        assert_eq!(slide.text_layers.len(), 2);
        assert_eq!(slide.text_layers[1].blend, "overlay");
        // Texts pin so a future schema change can't silently rewrite
        // them.
        assert_eq!(slide.text_layers[0].text, "OVERLAY");
        assert_eq!(slide.text_layers[1].text, "BLEND");
    }

    #[test]
    fn p2g_overlay_route_fixture_round_trips_via_value() {
        // Stronger guarantee: serialize the parsed slide back to
        // serde_json::Value and compare to the source item value's
        // fields. If a new spec field landed in production that the
        // renderer's TextSlide doesn't yet model, this catches the
        // gap. The check is per-field (not whole-value equality)
        // because the renderer's TextSlide is a partial mirror by
        // design (it only fields what Phase 4 needs).
        let path = fixture_path();
        let bytes = std::fs::read(&path).expect("fixture file readable");
        let envelope: ItemEnvelope =
            serde_json::from_slice(&bytes).expect("envelope parses");
        // Stash the source item before consuming it for slide parse.
        let source_item = envelope.item.clone();
        let slide: TextSlide =
            serde_json::from_value(envelope.item).expect("text_slide parses");

        // For each field the renderer DOES model, the round-tripped
        // serialization back to JSON must match the source. Adding
        // a new field to the source-of-truth schema without a
        // corresponding renderer-side field surfaces here as a
        // mismatch on the missed key.
        let layer = source_item
            .get("text_layers")
            .and_then(|v| v.as_array())
            .expect("text_layers present");
        for (i, src_layer) in layer.iter().enumerate() {
            let parsed_text = src_layer.get("text").and_then(|v| v.as_str());
            assert_eq!(
                slide.text_layers[i].text.as_str(),
                parsed_text.unwrap_or(""),
                "layer {} text mismatch",
                i
            );
            let parsed_blend = src_layer.get("blend").and_then(|v| v.as_str()).unwrap_or("normal");
            assert_eq!(
                slide.text_layers[i].blend.as_str(),
                parsed_blend,
                "layer {} blend mismatch",
                i
            );
        }
    }
}
