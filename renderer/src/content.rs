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

/// `<content_root>/<item_id>/`. Pulled out so tests can build the
/// same path without re-deriving the layout convention.
pub fn item_dir(content_root: &Path, item_id: Uuid) -> PathBuf {
    content_root.join(item_id.to_string())
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

    fn slide_with_pattern(pattern: &str, color_a: &str) -> TextSlide {
        TextSlide {
            id: Uuid::nil(),
            name: String::new(),
            duration_ms: 5000,
            background_color: "#222222".to_string(),
            background_pattern: Some(BackgroundPattern {
                pattern: pattern.to_string(),
                color_a: color_a.to_string(),
                color_b: "#FFFFFF".to_string(),
                density: 0.5,
            }),
        }
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
        };
        assert_eq!(solid_bg_hex(&slide), "#050608");
    }
}
