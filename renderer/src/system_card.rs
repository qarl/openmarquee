//! PR3 (2026-06-27) onboarding system cards — pure layout module.
//!
//! Takes `RenderSystemCardParams` from the IPC layer and produces a
//! `Vec<CardShape>` that describes the card's visual elements in
//! normalized (0..1) coordinates relative to card width/height. The
//! GLES2 paint code in `hdmi.rs` consumes the shape list + scales
//! to physical pixels at paint time.
//!
//! Pure module — no GL deps; host-runnable on macOS. The 5 card
//! layouts (SETUP / CONNECTING / CONNECTED / DEGRADED / BOOT) match
//! `qa/onboarding-marquee-cards-mockup.html` 1:1 — the mockup's
//! cqw units (container-query-width) translate directly to
//! normalized x.xx values here (e.g. 7cqw = 0.07 of card width).
//!
//! State machine: a single active card kept in the renderer's HDMI
//! session state. RenderSystemCard replaces the active card; the
//! per-frame paint loop checks the ttl-expiry monotonic and clears
//! the active card when the window elapses. ClearSystemCard
//! explicitly clears (the supervisor's ONLINE-state transition).

use crate::playback::{DegradedVariant, RenderSystemCardParams, SystemCardKind};

/// PR3 palette — pinned to the mockup at
/// `qa/onboarding-marquee-cards-mockup.html` :root vars.
/// Order is documented adjacent to its CSS counterpart so a future
/// brand tweak is a one-touch update.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Rgb(pub u8, pub u8, pub u8);

pub const BG: Rgb = Rgb(0x0e, 0x0e, 0x10); // --bg
pub const TEXT: Rgb = Rgb(0xe9, 0xe9, 0xec); // --text
pub const MUTED: Rgb = Rgb(0x8b, 0x8b, 0x92); // --muted
pub const ACCENT: Rgb = Rgb(0xff, 0xb4, 0x3c); // --om-accent (corrected LED-dot amber)
pub const ACCENT_INK: Rgb = Rgb(0x1a, 0x0f, 0x00); // --accent-ink
pub const SUCCESS: Rgb = Rgb(0x5d, 0xd3, 0x9e); // --success (green)
pub const DANGER: Rgb = Rgb(0xff, 0x6b, 0x6b); // --danger (red)
pub const QR_BG: Rgb = Rgb(0xff, 0xff, 0xff); // white panel
pub const QR_FG: Rgb = Rgb(0x00, 0x00, 0x00); // black modules

// Boot-card redesign (qarl mockups, 2026-07-16). The mockup's amber is
// #ffb84d, but ACCENT above is #ffb43c — the CORRECTED LED-dot amber
// (see the brand-mark source). Keeping the corrected value rather than
// regressing to the mockup's older swatch; the difference is a hair of
// warmth at LED gamma and the mark.png artwork is authored to #ffb43c.
/// Muted amber for the small-caps section labels ("— REACH THIS SIGN AT").
pub const ACCENT_DIM: Rgb = Rgb(0xe2, 0x9a, 0x2d); // mockup --accent-dim
/// Hairline rules: the divider + the QR corner brackets.
pub const RULE: Rgb = Rgb(0x26, 0x26, 0x2c); // mockup --rule
/// The bottom footer lockup — dimmest ink on the card.
pub const FOOTER_DIM: Rgb = Rgb(0x4a, 0x4a, 0x52); // mockup --footer

/// Which display font the layout requests. The GLES2 paint maps
/// these to the existing font_family_to_filename surface
/// (Oswald / Bebas Neue / Inter / JetBrains Mono).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DisplayFont {
    /// Big condensed display type for headlines (Oswald).
    Headline,
    /// Body / sub-text (Inter).
    Body,
    /// Monospace for address / PIN / IP (JetBrains Mono).
    Mono,
}

/// Horizontal text alignment within the shape's box.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Align {
    Left,
    Center,
    Right,
}

/// One paintable element of a card. Positions/sizes are normalized
/// (0..1) relative to the card's width × height. The GLES2 paint
/// scales these to physical pixels at draw time.
#[derive(Debug, Clone, PartialEq)]
pub enum CardShape {
    /// Solid fill of the card background (always at index 0). The
    /// renderer paints this first to ensure no playlist content
    /// bleeds through.
    Background { color: Rgb },
    // NOTE: there is intentionally NO `Monogram` variant. openMarquee has
    // ONE brand mark — the dot-matrix wordmark artwork (mark.png). The
    // old "oM" square (CardShape::Monogram) was DELETED 2026-07-15 by
    // qarl after it recurred; deleting the variant (not just skipping its
    // emission) is the structural prevention — a layout physically cannot
    // re-add it. Use `Image` (below) for the wordmark. See
    // feedback_no_om_monogram; do NOT reintroduce a monogram/oM-square.
    /// A solid axis-aligned rectangle in normalized card units — the
    /// hairline primitive the redesign needs (qarl mockups 2026-07-16):
    /// the divider above the tagline, and the QR's corner brackets
    /// (each bracket = two Rules, one horizontal + one vertical).
    ///
    /// Added because the card had no rect/line shape — everything else
    /// in the redesign reuses Text / Image / QrPanel. Paints as a plain
    /// filled quad (the same draw Background/Chip already use), so this
    /// is an additive primitive, not a layout-engine change.
    ///
    /// `size` is (width, height) as fractions of card WIDTH and HEIGHT
    /// respectively. A "line" is just a Rule with one tiny dimension —
    /// callers use `hairline_*` helpers so thickness stays in PIXELS-ish
    /// terms on either aspect rather than smearing on one axis.
    Rule { top_left: (f32, f32), size: (f32, f32), color: Rgb },
    /// A baked-in brand-mark image (the real splash wordmark, mark.png),
    /// blitted as a textured quad. `top_left` is the normalized card
    /// position; `height` is the image's height as a fraction of the card
    /// HEIGHT. The paint sizes the width from the texture's own aspect so
    /// the artwork is never distorted (see `MARK_ASPECT`).
    Image { top_left: (f32, f32), height: f32 },
    /// The state chip in the top-right corner. Solid-fill color +
    /// label.
    Chip {
        top_right: (f32, f32),
        label: String,
        bg: Rgb,
        ink: Rgb,
        text_height: f32,
    },
    /// A line / multi-line block of text. `max_height` is the EM size
    /// in normalized card-height units (one line) — the paint does
    /// `size_px = max_height * mode_h` and 1 em on-screen = size_px.
    /// (Was documented as "cap-height" until 2026-07-16; the impl
    /// always treated it as the em, and `mono_run_w` trusting the
    /// comment cost a 37% width over-estimate.) `text`
    /// can contain '\n' for line breaks; layout splits and stacks
    /// with line-height ~1.0.
    Text {
        anchor: (f32, f32),
        max_height: f32,
        color: Rgb,
        font: DisplayFont,
        align: Align,
        text: String,
    },
    /// The white QR panel + the QR module pattern. The renderer's
    /// paint reads the `qr_payload` from the params and encodes via
    /// `qr::encode_qr` at paint time (or pre-encoded into the shape
    /// — left to the paint layer). The shape carries the panel
    /// geometry + the payload string.
    QrPanel {
        top_left: (f32, f32),
        size: f32,
        payload: String,
        caption: String,
    },
    /// PR4 reserve: the rapid-boot hint line at the bottom of BOOT.
    /// PR3 leaves this empty (no boot_hint) so the slot exists
    /// without rendering anything.
    BootHint { center_bottom: (f32, f32), text: String, color: Rgb },
    /// Bottom footer bar (one centered line of muted text).
    /// Currently only the CONNECTED card uses it.
    Footer { text: String, color: Rgb, max_height: f32 },
    /// Animated spinner — the CONNECTING card's right-of-headline
    /// progress indicator. The paint code drives the rotation
    /// from monotonic time; the layout just declares position +
    /// size.
    Spinner { center: (f32, f32), radius: f32, color: Rgb },
}

/// PR3 active-card state held by the renderer (one per HDMI
/// session). Set on RenderSystemCard; cleared on ClearSystemCard
/// OR ttl-expiry detected by the paint loop OR the finish-pass
/// max-lifetime safety net.
#[derive(Debug, Clone)]
pub struct ActiveSystemCard {
    pub params: RenderSystemCardParams,
    pub shapes: Vec<CardShape>,
    /// Monotonic deadline (`Instant`) when the card auto-clears,
    /// OR None for "until-state-change" (supervisor must
    /// explicitly send ClearSystemCard or replace the card).
    pub deadline: Option<std::time::Instant>,
    /// PR3 finish-pass (2026-07-01) — safety net for `ttl_ms=None`
    /// cards. Even the "until-state-change" cards get force-cleared
    /// after `SYSTEM_CARD_MAX_LIFETIME_S` (see hdmi.rs) so a
    /// supervisor crash / missed-ClearSystemCard can never wedge
    /// the sign indefinitely.
    pub activated_at: std::time::Instant,
    /// PR3.1 (2026-07-01) — cache the encoded QR bitmap so the paint
    /// loop skips ~625 modules of encoder + palette work every
    /// frame while the card is active. Populated at construction
    /// (in ipc_main.rs's RenderSystemCard arm) when the params
    /// carry a non-empty qr_payload; None otherwise. Arc so the
    /// per-frame clone in the paint hook is O(1) refcount bump
    /// rather than O(N²) bitmap copy.
    pub qr_cache: Option<std::sync::Arc<crate::qr::QrBitmap>>,
}

/// PR3 finish-pass (2026-07-01) — per-field byte caps applied to
/// incoming `RenderSystemCardParams` before layout. Prevents a
/// chatty client from growing the per-frame shape-clone cost or
/// leaking long strings through the diagnostic ring buffer / QR
/// encoder / preview endpoint. Chosen to bound the actual visual
/// content:
///   * SSID: IEEE 802.11 caps SSID at 32 bytes; +8 headroom for
///     the preview endpoint's leading-space quirks.
///   * PIN: 2026-07-07 — this field carries the setup-AP JOIN
///     credential (SystemSettings.wifi_password), the same value the
///     QR encodes, NOT a short numeric per-boot PIN. WPA2 passphrases
///     run 8–63 chars, so the cap is the WPA2 max (63); a 12-byte cap
///     truncated the real password into an unjoinable string.
///   * QR payload: WIFI:T:WPA;S:<32>;P:<63>;; ≈ 128 bytes worst-
///     case; 256 gives forward-headroom for future URI schemes.
///   * addresses / target_ssid: 128 bytes covers even long mDNS
///     names + descriptive suffixes.
///   * boot_hint: PR4 reserves this for the rapid-boot line; 96
///     bytes fits any localised copy of "Restart 2× more…"
pub const MAX_SSID_LEN: usize = 40;
pub const MAX_PIN_LEN: usize = 63;
pub const MAX_QR_PAYLOAD_LEN: usize = 256;
pub const MAX_ADDRESS_LEN: usize = 128;
pub const MAX_IP_LEN: usize = 45; // IPv4 or IPv6 (INET6_ADDRSTRLEN is 46 incl. nul)
pub const MAX_BOOT_HINT_LEN: usize = 96;

fn clamp_opt_string(s: Option<String>, max: usize) -> Option<String> {
    s.map(|mut owned| {
        if owned.len() > max {
            // Split on a char boundary at or before `max` so a
            // multibyte UTF-8 sequence is never bisected.
            let cut = owned
                .char_indices()
                .take_while(|(idx, _)| *idx <= max)
                .last()
                .map(|(idx, _)| idx)
                .unwrap_or(0);
            owned.truncate(cut);
        }
        owned
    })
}

/// PR3 finish-pass (2026-07-01) — clamp every incoming string field
/// to a sane maximum. Called by the IPC handler before storing the
/// params on `ActiveSystemCard` so downstream consumers (paint,
/// diagnostics, preview) all see bounded strings.
pub fn clamp_params(mut p: RenderSystemCardParams) -> RenderSystemCardParams {
    p.ssid = clamp_opt_string(p.ssid, MAX_SSID_LEN);
    p.pin = clamp_opt_string(p.pin, MAX_PIN_LEN);
    p.qr_payload = clamp_opt_string(p.qr_payload, MAX_QR_PAYLOAD_LEN);
    p.address = clamp_opt_string(p.address, MAX_ADDRESS_LEN);
    p.ip = clamp_opt_string(p.ip, MAX_IP_LEN);
    p.target_ssid = clamp_opt_string(p.target_ssid, MAX_ADDRESS_LEN);
    p.boot_hint = clamp_opt_string(p.boot_hint, MAX_BOOT_HINT_LEN);
    p
}

/// Aspect ratio (width / height) of the baked mark artwork (mark.png,
/// 1618×517). The paint sizes the mark image by `height × MARK_ASPECT`, so
/// the artwork is never distorted; layout uses it to reserve horizontal
/// room for the mark when placing neighbouring text.
pub const MARK_ASPECT: f32 = 1618.0 / 517.0;

/// A BOOT card lays out in the landscape (2-column) form when the
/// framebuffer is wider than tall (aspect > 1). The FYS panel (portrait,
/// rot90) reports < 1; a landscape HDMI reports > 1. `mode_w`/`mode_h` are
/// already post-rotation, so this is the effective on-glass aspect.
fn is_landscape(aspect: f32) -> bool {
    aspect > 1.0
}

/// PR3 layout entrypoint: turn IPC params into the shape list. `aspect` is
/// the effective (post-rotation) framebuffer width/height — the BOOT card
/// adapts its layout to it (portrait vertical stack vs landscape 2-column).
/// Pure — no GL deps, no time deps; tests pin layout positions per kind.
pub fn layout_card(params: &RenderSystemCardParams, aspect: f32) -> Vec<CardShape> {
    let mut shapes = Vec::with_capacity(8);
    shapes.push(CardShape::Background { color: BG });
    match params.kind {
        // PR3 finish-pass forward-compat: `Unknown` (from a newer
        // backend) degrades to the SETUP layout — safe default:
        // AP-up + QR + PIN, so a user still has a path forward.
        SystemCardKind::Setup | SystemCardKind::Unknown => layout_setup(params, &mut shapes),
        SystemCardKind::Connecting => layout_connecting(params, &mut shapes),
        SystemCardKind::Connected => layout_connected(params, &mut shapes),
        SystemCardKind::Degraded => layout_degraded(params, &mut shapes),
        SystemCardKind::Boot => layout_boot(params, &mut shapes, aspect),
    }
    shapes
}

// === Per-kind layouts. All offsets in normalized 0..1 of card
// width/height. cqw -> normalized: divide by 100. ===

/// Brand-mark (dot-matrix wordmark, mark.png) lockup on every non-BOOT
/// card — top-left, the corner the deleted "oM" monogram used to occupy.
/// 2026-07-15 (qarl): the mark image replaces the RETIRED
/// `CardShape::Monogram` everywhere (openMarquee has ONE brand mark — the
/// wordmark; there is no oM-square. See feedback_no_om_monogram). `height`
/// is a fraction of card height; the paint sizes the width from
/// `MARK_ASPECT` so the artwork is never distorted.
const MARK_TL: (f32, f32) = (0.046, 0.046);
const MARK_HEIGHT: f32 = 0.05;
/// Chip top-right offset.
const CHIP_TR: (f32, f32) = (0.046, 0.046);
/// Chip's label cap-height (1.7cqw).
const CHIP_HEIGHT: f32 = 0.017;

fn layout_setup(params: &RenderSystemCardParams, shapes: &mut Vec<CardShape>) {
    shapes.push(CardShape::Image {
        top_left: MARK_TL,
        height: MARK_HEIGHT,
    });
    shapes.push(CardShape::Chip {
        top_right: CHIP_TR,
        label: "SETUP MODE".to_string(),
        bg: ACCENT,
        ink: ACCENT_INK,
        text_height: CHIP_HEIGHT,
    });
    // QR panel on the left of the mid row.
    let qr_payload = params.qr_payload.clone().unwrap_or_default();
    shapes.push(CardShape::QrPanel {
        top_left: (0.07, 0.30),
        size: 0.31,
        payload: qr_payload,
        caption: "Scan to join".to_string(),
    });
    // Headline.
    shapes.push(CardShape::Text {
        anchor: (0.46, 0.34),
        max_height: 0.07,
        color: ACCENT,
        font: DisplayFont::Headline,
        align: Align::Left,
        text: "Set up your\nmarquee".to_string(),
    });
    // KV strip.
    let ssid = params.ssid.as_deref().unwrap_or("openMarquee-Setup");
    let pin = params.pin.as_deref().unwrap_or("----");
    shapes.push(CardShape::Text {
        anchor: (0.46, 0.58),
        max_height: 0.025,
        color: TEXT,
        font: DisplayFont::Mono,
        align: Align::Left,
        // 2026-07-07 (qarl): the join credential is the full WPA2
        // passphrase, not a short PIN — label it "Password". Columns
        // re-aligned to the longer label (both values start at col 10).
        text: format!("Network   {}\nPassword  {}", ssid, pin),
    });
    // Steps.
    shapes.push(CardShape::Text {
        anchor: (0.46, 0.75),
        max_height: 0.023,
        color: MUTED,
        font: DisplayFont::Body,
        align: Align::Left,
        text: "Scan with your phone camera → the setup\npage opens automatically. No camera? Join\nthe network above and enter the password."
            .to_string(),
    });
    // 2026-07-02 (audit 4b close-out): if the supervisor threaded a
    // classified variant into the SETUP card (state machine routed
    // CONNECTING → SETUP via STA_AUTH_FAILED or STA_SSID_NOT_FOUND),
    // paint a reason banner across the bottom in DANGER color so the
    // operator standing at the sign knows WHY the last attempt
    // failed. On a fresh boot / no prior failure `params.variant`
    // is None and we skip the banner (the card renders identically
    // to the pre-close-out layout).
    if let Some(reason) = setup_reason_copy(params.variant, params.target_ssid.as_deref()) {
        shapes.push(CardShape::Text {
            anchor: (0.07, 0.90),
            max_height: 0.026,
            color: DANGER,
            font: DisplayFont::Body,
            align: Align::Left,
            text: reason,
        });
    }
}

/// 2026-07-02 (audit 4b close-out) SETUP-card reason banner copy.
/// Pure function; returns `None` when the variant carries no useful
/// signal for a first-connect failure (Lost / Unknown / absent),
/// so the caller can skip the banner cleanly.
pub fn setup_reason_copy(
    variant: Option<DegradedVariant>,
    target_ssid: Option<&str>,
) -> Option<String> {
    let ssid = target_ssid.unwrap_or("the wifi");
    match variant {
        Some(DegradedVariant::AuthFail) => Some(
            "Last attempt: password rejected. Re-scan the QR and enter the correct wifi password.".to_string(),
        ),
        Some(DegradedVariant::NotFound) => Some(format!(
            "Last attempt: \u{201C}{}\u{201D} not in range. Check the network name and the router.",
            ssid
        )),
        Some(DegradedVariant::NotFoundOr5ghz) => Some(format!(
            "Last attempt: \u{201C}{}\u{201D} is 5 GHz only. This device needs a 2.4 GHz network.",
            ssid
        )),
        // Lost + Unknown + None: no first-connect story to tell —
        // Lost means the STA disconnected AFTER connecting (never
        // happens during onboarding SETUP entry), Unknown is a
        // forward-compat catch-all, None is the fresh-boot case.
        _ => None,
    }
}

fn layout_connecting(params: &RenderSystemCardParams, shapes: &mut Vec<CardShape>) {
    shapes.push(CardShape::Image {
        top_left: MARK_TL,
        height: MARK_HEIGHT,
    });
    shapes.push(CardShape::Chip {
        top_right: CHIP_TR,
        label: "CONNECTING".to_string(),
        bg: ACCENT,
        ink: ACCENT_INK,
        text_height: CHIP_HEIGHT,
    });
    let target = params.target_ssid.as_deref().unwrap_or("home network");
    shapes.push(CardShape::Text {
        anchor: (0.07, 0.40),
        max_height: 0.07,
        color: ACCENT,
        font: DisplayFont::Headline,
        align: Align::Left,
        text: format!("Joining \u{201C}{}\u{201D}\u{2026}", target),
    });
    shapes.push(CardShape::Spinner {
        center: (0.86, 0.40),
        radius: 0.026,
        color: ACCENT,
    });
    shapes.push(CardShape::Text {
        anchor: (0.07, 0.52),
        max_height: 0.025,
        color: MUTED,
        font: DisplayFont::Body,
        align: Align::Left,
        text: "The setup network may blink for a few seconds — that\u{2019}s normal. Stay on this page."
            .to_string(),
    });
}

fn layout_connected(params: &RenderSystemCardParams, shapes: &mut Vec<CardShape>) {
    shapes.push(CardShape::Image {
        top_left: MARK_TL,
        height: MARK_HEIGHT,
    });
    shapes.push(CardShape::Chip {
        top_right: CHIP_TR,
        label: "\u{2713} CONNECTED".to_string(),
        bg: SUCCESS,
        ink: Rgb(0x06, 0x2b, 0x1d),
        text_height: CHIP_HEIGHT,
    });
    shapes.push(CardShape::Text {
        anchor: (0.07, 0.32),
        max_height: 0.07,
        color: SUCCESS,
        font: DisplayFont::Headline,
        align: Align::Left,
        text: "You\u{2019}re connected".to_string(),
    });
    shapes.push(CardShape::Text {
        anchor: (0.07, 0.46),
        max_height: 0.025,
        color: MUTED,
        font: DisplayFont::Body,
        align: Align::Left,
        text: "Find your marquee at:".to_string(),
    });
    let address = params.address.as_deref().unwrap_or("openmarquee.local");
    shapes.push(CardShape::Text {
        anchor: (0.07, 0.55),
        max_height: 0.05,
        color: ACCENT,
        font: DisplayFont::Mono,
        align: Align::Left,
        text: address.to_string(),
    });
    let ip = params.ip.as_deref().unwrap_or("");
    if !ip.is_empty() {
        shapes.push(CardShape::Text {
            anchor: (0.07, 0.66),
            max_height: 0.027,
            color: MUTED,
            font: DisplayFont::Mono,
            align: Align::Left,
            text: ip.to_string(),
        });
    }
    shapes.push(CardShape::Footer {
        text: "Setup network turns off shortly — reach the marquee at the address above."
            .to_string(),
        color: MUTED,
        max_height: 0.020,
    });
}

fn layout_degraded(params: &RenderSystemCardParams, shapes: &mut Vec<CardShape>) {
    shapes.push(CardShape::Image {
        top_left: MARK_TL,
        height: MARK_HEIGHT,
    });
    shapes.push(CardShape::Chip {
        top_right: CHIP_TR,
        label: "\u{26A0} OFFLINE".to_string(),
        bg: DANGER,
        ink: Rgb(0x3a, 0x08, 0x08),
        text_height: CHIP_HEIGHT,
    });
    let qr_payload = params.qr_payload.clone().unwrap_or_default();
    shapes.push(CardShape::QrPanel {
        top_left: (0.07, 0.30),
        size: 0.31,
        payload: qr_payload,
        caption: "Scan to fix".to_string(),
    });
    let (headline, sub) = degraded_copy(params.variant, params.target_ssid.as_deref());
    shapes.push(CardShape::Text {
        anchor: (0.46, 0.34),
        max_height: 0.054,
        color: DANGER,
        font: DisplayFont::Headline,
        align: Align::Left,
        text: headline,
    });
    let ssid = params.ssid.as_deref().unwrap_or("openMarquee-Setup");
    let pin = params.pin.as_deref().unwrap_or("----");
    let steps = format!(
        "{sub}\n\nTo fix: join {ssid}, password {pin}, re-enter wifi.",
        sub = sub,
        ssid = ssid,
        pin = pin,
    );
    shapes.push(CardShape::Text {
        anchor: (0.46, 0.58),
        max_height: 0.023,
        color: MUTED,
        font: DisplayFont::Body,
        align: Align::Left,
        text: steps,
    });
}

/// DEGRADED variant -> (headline, sub-copy) per the mockup's three
/// variants. Pure function, easily testable.
pub fn degraded_copy(
    variant: Option<DegradedVariant>,
    target_ssid: Option<&str>,
) -> (String, String) {
    match variant {
        Some(DegradedVariant::AuthFail) => (
            "WiFi password\nno longer works".to_string(),
            "The router rejected the saved password.".to_string(),
        ),
        Some(DegradedVariant::NotFound) => {
            let ssid = target_ssid.unwrap_or("the wifi");
            (
                format!("Can\u{2019}t find\n{}", ssid),
                "Router off, out of range, or the network name changed.".to_string(),
            )
        }
        Some(DegradedVariant::NotFoundOr5ghz) => {
            let ssid = target_ssid.unwrap_or("the wifi");
            (
                format!("Can\u{2019}t find\n{}\non 2.4 GHz", ssid),
                "This device is 2.4 GHz only. Check the router\u{2019}s 2.4 GHz radio."
                    .to_string(),
            )
        }
        Some(DegradedVariant::Lost | DegradedVariant::Unknown) | None => (
            "Lost the wifi\nconnection".to_string(),
            "The signal dropped or the router rebooted.".to_string(),
        ),
    }
}

/// Width of `n` JetBrains-Mono characters as a fraction of card WIDTH,
/// given `em_h` — the run's `max_height`, a fraction of card HEIGHT.
///
/// The pure layout layer has no font metrics (the paint measures via
/// `layout_text_to_quads`), but Mono is — by definition — a uniform
/// advance, so a run's width is analytic. JetBrains Mono: unitsPerEm
/// 1000, advance 600 → advance = 0.600 em.
///
/// `max_height` is the EM, not the cap-height: the paint does
/// `size_px = max_height * mode_h` and hdmi_logic documents "1 em
/// on-screen = size_px px". (The `CardShape::Text` doc comment said
/// "cap-height" — a stale doc/impl mismatch, corrected there. Trusting
/// it cost a 37% over-estimate here: source is the authority.)
///
/// Height fractions are of card HEIGHT and width fractions of card
/// WIDTH, hence the `/ aspect` to cross between the two axes.
///
/// Used ONLY to place the two-colour URL's split point (grey `http://`
/// + amber host). It is not a general text measurer — anything
/// non-Mono must keep using Align on the paint side. It models the
/// ADVANCE width; the paint places by the ink bbox, so ~1-2px of
/// residual is expected and invisible at these sizes.
pub fn mono_run_w(n: usize, em_h: f32, aspect: f32) -> f32 {
    const ADV_PER_EM: f32 = 0.600;
    if !(aspect.is_finite() && aspect > 0.0) {
        return 0.0;
    }
    n as f32 * ADV_PER_EM * em_h / aspect
}

/// `http://` — the fixed grey prefix of the two-colour URL.
const URL_SCHEME: &str = "http://";

/// Width the two-colour URL may occupy, as a fraction of card WIDTH.
const URL_BAND_W_LANDSCAPE: f32 = 0.50; // the left column
const URL_BAND_W_PORTRAIT: f32 = 0.90; // near-full width, centred

/// Cap-height for an `n`-char Mono run that fits `band_w`, never
/// exceeding `nominal`.
///
/// Splitting the URL into two coloured runs took it off the paint's
/// single-run fitting path, so the layout must own the fit: a long sign
/// name (`a-very-long-sign-name.local`) or a narrow panel would
/// otherwise push the run off-card — the bounds test caught exactly
/// that (a negative split anchor at aspect 0.4).
pub fn fit_mono_h(n: usize, nominal: f32, band_w: f32, aspect: f32) -> f32 {
    let natural = mono_run_w(n, nominal, aspect);
    if natural > band_w && natural > 0.0 {
        nominal * (band_w / natural)
    } else {
        nominal
    }
}

/// Split an address into (scheme, host) for the two-colour URL. The
/// address the backend hands us is a bare host (`jasonssign1.local`);
/// the mockup renders a muted `http://` in front of an amber host.
/// Tolerates an address that already carries a scheme.
fn split_url(address: &str) -> (&'static str, String) {
    let host = address
        .strip_prefix("http://")
        .or_else(|| address.strip_prefix("https://"))
        .unwrap_or(address);
    (URL_SCHEME, host.to_string())
}

/// Small-caps label text. The card fonts have no letter-spacing knob,
/// so the mockup's tracked small-caps are approximated by upper-casing
/// at a small size — the hierarchy reads, the tracking is a hair
/// tighter than the mockup. Flagged as a known approximation.
fn small_caps(s: &str) -> String {
    s.to_uppercase()
}

/// The QR corner brackets from the mockup: an L at two opposite corners
/// of the panel, drawn as hairline Rules. `arm` is the bracket arm
/// length as a fraction of the panel's WIDTH.
fn qr_brackets(
    top_left: (f32, f32),
    size_w: f32,
    size_h: f32,
    aspect: f32,
    shapes: &mut Vec<CardShape>,
) {
    let arm = size_w * 0.16;
    // Hairline: ~2px on a 1080-tall panel, expressed on each axis.
    let t_h = 0.0022; // thickness as a fraction of HEIGHT
    let t_w = t_h / aspect.max(0.0001); // same visual thickness on X
    let (x, y) = top_left;
    let gap_w = size_w * 0.045;
    let gap_h = gap_w * aspect;
    // Top-left L (sits just outside the panel).
    let lx = x - gap_w;
    let ly = y - gap_h;
    shapes.push(CardShape::Rule { top_left: (lx, ly), size: (arm, t_h), color: ACCENT_DIM });
    shapes.push(CardShape::Rule {
        top_left: (lx, ly),
        size: (t_w, arm * aspect),
        color: ACCENT_DIM,
    });
    // Bottom-right L.
    let rx = x + size_w + gap_w;
    let ry = y + size_h + gap_h;
    shapes.push(CardShape::Rule {
        top_left: (rx - arm, ry - t_h),
        size: (arm, t_h),
        color: ACCENT_DIM,
    });
    shapes.push(CardShape::Rule {
        top_left: (rx - t_w, ry - arm * aspect),
        size: (t_w, arm * aspect),
        color: ACCENT_DIM,
    });
}

fn layout_boot(params: &RenderSystemCardParams, shapes: &mut Vec<CardShape>, aspect: f32) {
    // BOOT identity card. The real dot-matrix wordmark (mark.png) is
    // blitted as an Image; the mDNS URL + wlan0 IP + a QR of the URL give a
    // viewer a way to reach the sign, and the Wi-Fi line names the network.
    //
    // Layout adapts to the framebuffer aspect (qarl 2026-07-07):
    //   * portrait (FYS panel, rot90): centred vertical stack.
    //   * landscape (HDMI): two columns — identity/URL/IP/Wi-Fi on the
    //     LEFT, the QR on the RIGHT — to fill the width.
    // The QR is "square-corrected": QrPanel `size` is a WIDTH fraction but
    // the paint makes the panel square in PIXELS (h_px = w_px = size*mode_w),
    // so `size = target_height_frac / aspect` gives a QR that occupies a
    // fixed fraction of HEIGHT on any aspect (never a giant/tiny square).
    //
    // Guard: an unstamped/invalid aspect (0.0 before the IPC loop stamps it,
    // or NaN/inf) falls back to the FYS portrait panel so the math is safe.
    let aspect = if aspect.is_finite() && aspect > 0.0 {
        aspect
    } else {
        768.0 / 1360.0
    };
    let address = params.address.as_deref().unwrap_or("openmarquee.local");
    let ip = params.ip.as_deref().unwrap_or("");
    let ssid = params.ssid.as_deref().filter(|s| !s.is_empty());
    let qr_payload = params.qr_payload.clone().unwrap_or_default();

    let (scheme, host) = split_url(address);

    if is_landscape(aspect) {
        // ---- Landscape: 2-column (mockup: /tmp/boot-card-redesign) ----
        const COL_X: f32 = 0.07;
        // Mark: top of the left column. `height` is a fraction of card
        // height; the paint sizes the width from the artwork aspect so it's
        // undistorted. (Wordmark mark.png — NOT the oM monogram: the
        // mockups draw a monogram tile, but that is a closed decision;
        // the card uses the one brand mark.)
        let mark_h = 0.13;
        shapes.push(CardShape::Image { top_left: (COL_X, 0.09), height: mark_h });

        // "— REACH THIS SIGN AT" — muted-amber small-caps section label.
        shapes.push(CardShape::Text {
            anchor: (COL_X, 0.30),
            max_height: 0.022,
            color: ACCENT_DIM,
            font: DisplayFont::Mono,
            align: Align::Left,
            text: format!("— {}", small_caps("Reach this sign at")),
        });

        // Two-colour URL: muted `http://` + amber host. Mono is uniform-
        // advance, so the scheme's width is analytic — right-align the
        // scheme at the split and left-align the host at the SAME point
        // and they meet exactly, with the run's left edge landing on COL_X.
        //
        // AUTO-FIT: a long sign name (or a narrow aspect) would push the
        // run past the column, so shrink the cap-height until the whole
        // `http://host` fits the band. Splitting the URL into two runs
        // removed the paint's single-run fitting, so the layout owns it.
        let url_h = fit_mono_h(
            scheme.len() + host.chars().count(),
            0.075,
            URL_BAND_W_LANDSCAPE,
            aspect,
        );
        let split_x = COL_X + mono_run_w(scheme.len(), url_h, aspect);
        shapes.push(CardShape::Text {
            anchor: (split_x, 0.40),
            max_height: url_h,
            color: MUTED,
            font: DisplayFont::Mono,
            align: Align::Right,
            text: scheme.to_string(),
        });
        shapes.push(CardShape::Text {
            anchor: (split_x, 0.40),
            max_height: url_h,
            color: ACCENT,
            font: DisplayFont::Mono,
            align: Align::Left,
            text: host.clone(),
        });

        // Labelled rows: small-caps side-label + value.
        const LABEL_X: f32 = COL_X;
        const VALUE_X: f32 = 0.135;
        if !ip.is_empty() {
            shapes.push(CardShape::Text {
                anchor: (LABEL_X, 0.545),
                max_height: 0.017,
                color: MUTED,
                font: DisplayFont::Mono,
                align: Align::Left,
                text: small_caps("Ip"),
            });
            shapes.push(CardShape::Text {
                anchor: (VALUE_X, 0.545),
                max_height: 0.035,
                color: TEXT,
                font: DisplayFont::Mono,
                align: Align::Left,
                text: ip.to_string(),
            });
        }
        if let Some(ssid) = ssid {
            shapes.push(CardShape::Text {
                anchor: (LABEL_X, 0.625),
                max_height: 0.017,
                color: MUTED,
                font: DisplayFont::Mono,
                align: Align::Left,
                text: small_caps("Wi-Fi"),
            });
            shapes.push(CardShape::Text {
                anchor: (VALUE_X, 0.625),
                max_height: 0.035,
                color: TEXT,
                font: DisplayFont::Mono,
                align: Align::Left,
                text: ssid.to_string(),
            });
        }

        // Divider above the tagline block.
        shapes.push(CardShape::Rule {
            top_left: (COL_X, 0.775),
            size: (0.33, 0.0018),
            color: RULE,
        });
        // Tagline — the human sentence. The last clause is the promise.
        shapes.push(CardShape::Text {
            anchor: (COL_X, 0.815),
            max_height: 0.024,
            color: MUTED,
            font: DisplayFont::Body,
            align: Align::Left,
            text: "Scan the code, or type the address into any browser on\nthis network. No app, no cloud, no account.".to_string(),
        });
        // Footer lockup.
        shapes.push(CardShape::Text {
            anchor: (COL_X, 0.945),
            max_height: 0.015,
            color: FOOTER_DIM,
            font: DisplayFont::Mono,
            align: Align::Left,
            text: format!("— {} · {}", small_caps("openmarquee"), small_caps("handmade led sign")),
        });

        // QR: right column, vertically centred + square-corrected.
        if !qr_payload.is_empty() {
            let qr_h_frac = 0.52;
            // Cap at the right-column band WIDTH (0.36), not a larger value:
            // on a near-square landscape (aspect ~1.0-1.3, e.g. a 4:3/5:4
            // monitor) qr_h_frac/aspect exceeds 0.36, and a wider panel at
            // qr_x=0.60 would clip past the framebuffer's right edge →
            // dropped modules → unscannable QR.
            let qr_size = (qr_h_frac / aspect).min(0.36); // normalized width
            // Centre the QR within the right column band [0.60, 0.96].
            let qr_x = 0.60 + ((0.36 - qr_size).max(0.0)) / 2.0;
            let qr_y = (1.0 - qr_h_frac) / 2.0;
            // "• SCAN TO OPEN" header above the panel.
            shapes.push(CardShape::Text {
                anchor: (qr_x + qr_size / 2.0, qr_y - 0.075),
                max_height: 0.022,
                color: ACCENT,
                font: DisplayFont::Mono,
                align: Align::Center,
                text: format!("• {}", small_caps("Scan to open")),
            });
            qr_brackets((qr_x, qr_y), qr_size, qr_h_frac, aspect, shapes);
            shapes.push(CardShape::QrPanel {
                top_left: (qr_x, qr_y),
                size: qr_size,
                payload: qr_payload,
                // Caption is drawn as its own Text below (two-tone copy);
                // the panel's built-in caption stays empty.
                caption: String::new(),
            });
            // Subtitle under the QR.
            shapes.push(CardShape::Text {
                anchor: (qr_x + qr_size / 2.0, qr_y + qr_h_frac + 0.06),
                max_height: 0.022,
                color: MUTED,
                font: DisplayFont::Body,
                align: Align::Center,
                text: format!("Opens {host} in your browser"),
            });
        }
    } else {
        // ---- Portrait: centred vertical stack (the FYS-panel layout) ----
        // Mark centred near the top, sized to ~0.62 of card width (height
        // derived from the artwork aspect + the panel aspect).
        let mark_w_frac: f32 = 0.52;
        let mark_h = (mark_w_frac * aspect / MARK_ASPECT).min(0.10);
        let mark_w = mark_h * MARK_ASPECT / aspect; // actual width after any clamp
        shapes.push(CardShape::Image {
            top_left: ((1.0 - mark_w) / 2.0, 0.045),
            height: mark_h,
        });

        // QR: the hero of the portrait card, with its header + brackets.
        // All three are guarded together — a card with no qr_payload
        // shows no "SCAN TO OPEN" promise and no empty brackets, and the
        // stack below closes up (matches the landscape branch's guard).
        const BOOT_QR_SIZE: f32 = 0.74; // fraction of WIDTH
        let qr_y = 0.19;
        let has_qr = !qr_payload.is_empty();
        let qr_bottom = if has_qr {
            let qr_x = 0.5 - BOOT_QR_SIZE / 2.0;
            let qr_h = BOOT_QR_SIZE * aspect; // panel is square in pixels
            // "— SCAN TO OPEN —" (em-dashes both sides, centred).
            shapes.push(CardShape::Text {
                anchor: (0.50, 0.145),
                max_height: 0.018,
                color: ACCENT,
                font: DisplayFont::Mono,
                align: Align::Center,
                text: format!("— {} —", small_caps("Scan to open")),
            });
            qr_brackets((qr_x, qr_y), BOOT_QR_SIZE, qr_h, aspect, shapes);
            shapes.push(CardShape::QrPanel {
                top_left: (qr_x, qr_y),
                size: BOOT_QR_SIZE,
                payload: qr_payload,
                caption: String::new(),
            });
            qr_y + qr_h
        } else {
            qr_y
        };

        // Two-colour URL BELOW the QR (mockup's portrait arrangement).
        // Centred: the run spans [split - scheme_w, split + host_w], so
        // centring the WHOLE run puts the split left of centre by half
        // the difference. Both widths are analytic in Mono.
        // AUTO-FIT (see fit_mono_h): keeps a long sign name on-card.
        let url_h = fit_mono_h(
            scheme.len() + host.chars().count(),
            0.036,
            URL_BAND_W_PORTRAIT,
            aspect,
        );
        let scheme_w = mono_run_w(scheme.len(), url_h, aspect);
        let host_w = mono_run_w(host.chars().count(), url_h, aspect);
        let split_x = 0.5 - (host_w - scheme_w) / 2.0;
        let url_y = qr_bottom + 0.055;
        shapes.push(CardShape::Text {
            anchor: (split_x, url_y),
            max_height: url_h,
            color: MUTED,
            font: DisplayFont::Mono,
            align: Align::Right,
            text: scheme.to_string(),
        });
        shapes.push(CardShape::Text {
            anchor: (split_x, url_y),
            max_height: url_h,
            color: ACCENT,
            font: DisplayFont::Mono,
            align: Align::Left,
            text: host.clone(),
        });
        shapes.push(CardShape::Text {
            anchor: (0.50, url_y + 0.037),
            max_height: 0.017,
            color: MUTED,
            font: DisplayFont::Mono,
            align: Align::Center,
            text: small_caps("Or type this into any browser"),
        });

        // Divider, then the two-column IP / WI-FI block.
        let rule_y = url_y + 0.078;
        shapes.push(CardShape::Rule {
            top_left: (0.07, rule_y),
            size: (0.86, 0.0012),
            color: RULE,
        });
        let row_y = rule_y + 0.028;
        if !ip.is_empty() {
            shapes.push(CardShape::Text {
                anchor: (0.07, row_y),
                max_height: 0.014,
                color: MUTED,
                font: DisplayFont::Mono,
                align: Align::Left,
                text: small_caps("Ip address"),
            });
            shapes.push(CardShape::Text {
                anchor: (0.07, row_y + 0.028),
                max_height: 0.026,
                color: TEXT,
                font: DisplayFont::Mono,
                align: Align::Left,
                text: ip.to_string(),
            });
        }
        if let Some(ssid) = ssid {
            shapes.push(CardShape::Text {
                anchor: (0.52, row_y),
                max_height: 0.014,
                color: MUTED,
                font: DisplayFont::Mono,
                align: Align::Left,
                text: small_caps("Wi-Fi"),
            });
            shapes.push(CardShape::Text {
                anchor: (0.52, row_y + 0.028),
                max_height: 0.026,
                color: TEXT,
                font: DisplayFont::Mono,
                align: Align::Left,
                text: ssid.to_string(),
            });
        }

        // Footer lockup.
        shapes.push(CardShape::Text {
            anchor: (0.50, 0.965),
            max_height: 0.013,
            color: FOOTER_DIM,
            font: DisplayFont::Mono,
            align: Align::Center,
            text: format!(
                "{} • {} • {}",
                small_caps("Handmade led sign"),
                small_caps("No app"),
                small_caps("No cloud"),
            ),
        });
    }
    // Rapid-boot hint line (both layouts).
    if let Some(hint) = params.boot_hint.as_deref() {
        if !hint.is_empty() {
            shapes.push(CardShape::BootHint {
                center_bottom: (0.50, 0.954),
                text: hint.to_string(),
                color: ACCENT,
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Aspect (width/height) fixtures: the FYS panel is portrait (768×1360
    // rot90), a landscape HDMI is 1920×1080. Both are post-rotation, as
    // layout_card expects.
    const PORTRAIT: f32 = 768.0 / 1360.0;
    const LANDSCAPE: f32 = 1920.0 / 1080.0;

    /// Assert every geometric shape stays within the card [0,1]×[0,1],
    /// accounting for the aspect-derived real extents: the mark Image's
    /// width = height·MARK_ASPECT/aspect, and the QrPanel is square in
    /// PIXELS so its normalized height = size·aspect.
    fn assert_shapes_in_bounds(shapes: &[CardShape], aspect: f32) {
        let inb = |v: f32| (-0.002..=1.002).contains(&v);
        for s in shapes {
            match s {
                CardShape::Image { top_left, height } => {
                    let w = height * MARK_ASPECT / aspect;
                    assert!(
                        inb(top_left.0) && inb(top_left.0 + w) && inb(top_left.1) && inb(top_left.1 + height),
                        "Image out of bounds: tl={top_left:?} h={height} w={w} aspect={aspect}"
                    );
                }
                CardShape::QrPanel { top_left, size, .. } => {
                    let h = size * aspect;
                    assert!(
                        inb(top_left.0) && inb(top_left.0 + size) && inb(top_left.1) && inb(top_left.1 + h),
                        "QrPanel out of bounds: tl={top_left:?} size={size} h={h} aspect={aspect}"
                    );
                }
                CardShape::Text { anchor, .. } => {
                    assert!(inb(anchor.0) && inb(anchor.1), "Text anchor out of bounds: {anchor:?}");
                }
                CardShape::BootHint { center_bottom, .. } => {
                    assert!(inb(center_bottom.0) && inb(center_bottom.1), "BootHint out of bounds: {center_bottom:?}");
                }
                // The redesign's hairline primitive — the divider + the
                // QR brackets. Without this arm the `_ => {}` fallthrough
                // left Rule entirely unchecked (sacred review 2026-07-16).
                CardShape::Rule { top_left, size, .. } => {
                    assert!(
                        inb(top_left.0) && inb(top_left.0 + size.0)
                            && inb(top_left.1) && inb(top_left.1 + size.1),
                        "Rule out of bounds: tl={top_left:?} size={size:?} aspect={aspect}"
                    );
                }
                _ => {}
            }
        }
    }
    use crate::playback::{DegradedVariant, RenderSystemCardParams, SystemCardKind};

    fn params(kind: SystemCardKind) -> RenderSystemCardParams {
        RenderSystemCardParams {
            kind,
            ssid: None,
            pin: None,
            qr_payload: None,
            address: None,
            ip: None,
            target_ssid: None,
            variant: None,
            ttl_ms: None,
            boot_hint: None,
        }
    }

    #[test]
    fn every_kind_emits_background_first() {
        for kind in [
            SystemCardKind::Setup,
            SystemCardKind::Connecting,
            SystemCardKind::Connected,
            SystemCardKind::Degraded,
            SystemCardKind::Boot,
        ] {
            let shapes = layout_card(&params(kind), PORTRAIT);
            assert!(
                matches!(shapes[0], CardShape::Background { color: BG }),
                "kind={:?} must emit BG fill at index 0",
                kind
            );
        }
    }

    #[test]
    fn setup_no_variant_omits_reason_banner() {
        // 2026-07-02 (audit 4b close-out): a SETUP card with no
        // classified variant (fresh boot, no prior failure) must
        // render identically to the pre-close-out layout — no
        // DANGER-colored Text shapes anywhere.
        let p = params(SystemCardKind::Setup);
        let shapes = layout_card(&p, PORTRAIT);
        assert!(
            !shapes.iter().any(|s| matches!(
                s,
                CardShape::Text { color: DANGER, .. }
            )),
            "SETUP without variant must NOT emit a DANGER-colored reason banner"
        );
    }

    #[test]
    fn setup_auth_fail_variant_paints_password_reason() {
        let mut p = params(SystemCardKind::Setup);
        p.variant = Some(DegradedVariant::AuthFail);
        p.target_ssid = Some("HomeWifi".to_string());
        let shapes = layout_card(&p, PORTRAIT);
        let banner = shapes
            .iter()
            .find_map(|s| match s {
                CardShape::Text { text, color: DANGER, .. } => Some(text.as_str()),
                _ => None,
            })
            .expect("SETUP + auth_fail must emit a DANGER-colored reason banner");
        assert!(
            banner.to_lowercase().contains("password"),
            "auth_fail banner must name the password; got {:?}",
            banner
        );
    }

    #[test]
    fn setup_not_found_variant_paints_ssid_in_reason() {
        let mut p = params(SystemCardKind::Setup);
        p.variant = Some(DegradedVariant::NotFound);
        p.target_ssid = Some("HomeWifi".to_string());
        let shapes = layout_card(&p, PORTRAIT);
        let banner = shapes
            .iter()
            .find_map(|s| match s {
                CardShape::Text { text, color: DANGER, .. } => Some(text.as_str()),
                _ => None,
            })
            .expect("SETUP + not_found must emit a DANGER-colored reason banner");
        assert!(
            banner.contains("HomeWifi"),
            "not_found banner must name the target SSID; got {:?}",
            banner
        );
    }

    #[test]
    fn setup_not_found_or_5ghz_variant_calls_out_5ghz() {
        let mut p = params(SystemCardKind::Setup);
        p.variant = Some(DegradedVariant::NotFoundOr5ghz);
        p.target_ssid = Some("HomeWifi".to_string());
        let shapes = layout_card(&p, PORTRAIT);
        let banner = shapes
            .iter()
            .find_map(|s| match s {
                CardShape::Text { text, color: DANGER, .. } => Some(text.as_str()),
                _ => None,
            })
            .expect("SETUP + not_found_or_5ghz must emit a DANGER banner");
        assert!(
            banner.contains("5 GHz") && banner.contains("2.4 GHz"),
            "5 GHz banner must name both bands; got {:?}",
            banner
        );
        assert!(banner.contains("HomeWifi"));
    }

    #[test]
    fn setup_lost_variant_omits_reason_banner() {
        // Lost means STA dropped AFTER connecting — nonsensical on the
        // SETUP card (first-connect story). setup_reason_copy returns
        // None so no banner paints, even though the variant is set.
        let mut p = params(SystemCardKind::Setup);
        p.variant = Some(DegradedVariant::Lost);
        let shapes = layout_card(&p, PORTRAIT);
        assert!(
            !shapes.iter().any(|s| matches!(
                s,
                CardShape::Text { color: DANGER, .. }
            )),
            "SETUP + Lost variant must NOT paint a first-connect banner"
        );
    }

    #[test]
    fn setup_carries_qr_payload_through() {
        let mut p = params(SystemCardKind::Setup);
        p.qr_payload = Some("WIFI:T:WPA;S:openMarquee-Setup;P:4827;;".to_string());
        let shapes = layout_card(&p, PORTRAIT);
        let qr = shapes
            .iter()
            .find_map(|s| match s {
                CardShape::QrPanel { payload, .. } => Some(payload.as_str()),
                _ => None,
            })
            .expect("SETUP must emit a QrPanel");
        assert_eq!(qr, "WIFI:T:WPA;S:openMarquee-Setup;P:4827;;");
    }

    #[test]
    fn setup_renders_ssid_and_pin_in_kv_text() {
        let mut p = params(SystemCardKind::Setup);
        p.ssid = Some("MyNet".to_string());
        p.pin = Some("1234".to_string());
        let shapes = layout_card(&p, PORTRAIT);
        let kv_text = shapes
            .iter()
            .filter_map(|s| match s {
                CardShape::Text { text, font: DisplayFont::Mono, .. } => Some(text.as_str()),
                _ => None,
            })
            .collect::<Vec<_>>();
        assert!(
            kv_text.iter().any(|t| t.contains("MyNet") && t.contains("1234")),
            "expected SSID + Password in a mono Text shape; got {:?}",
            kv_text
        );
    }

    #[test]
    fn connecting_substitutes_target_ssid_in_headline() {
        let mut p = params(SystemCardKind::Connecting);
        p.target_ssid = Some("CafeWiFi".to_string());
        let shapes = layout_card(&p, PORTRAIT);
        let headline = shapes
            .iter()
            .find_map(|s| match s {
                CardShape::Text { text, font: DisplayFont::Headline, .. } => Some(text.as_str()),
                _ => None,
            })
            .expect("CONNECTING must emit a Headline Text");
        assert!(
            headline.contains("CafeWiFi"),
            "expected 'Joining \"CafeWiFi\"…' in headline; got {:?}",
            headline
        );
    }

    #[test]
    fn connecting_emits_spinner() {
        let shapes = layout_card(&params(SystemCardKind::Connecting), PORTRAIT);
        assert!(
            shapes.iter().any(|s| matches!(s, CardShape::Spinner { .. })),
            "CONNECTING must emit a Spinner shape"
        );
    }

    #[test]
    fn connected_shows_address_and_ip() {
        let mut p = params(SystemCardKind::Connected);
        p.address = Some("foo.local".to_string());
        p.ip = Some("10.0.0.5".to_string());
        let shapes = layout_card(&p, PORTRAIT);
        let mono_texts: Vec<&str> = shapes
            .iter()
            .filter_map(|s| match s {
                CardShape::Text { text, font: DisplayFont::Mono, .. } => Some(text.as_str()),
                _ => None,
            })
            .collect();
        assert!(mono_texts.contains(&"foo.local"));
        assert!(mono_texts.contains(&"10.0.0.5"));
    }

    #[test]
    fn connected_omits_ip_shape_when_ip_is_none() {
        let mut p = params(SystemCardKind::Connected);
        p.address = Some("foo.local".to_string());
        let shapes = layout_card(&p, PORTRAIT);
        // No mono Text shape that contains an IP-shaped string.
        let mono_count = shapes
            .iter()
            .filter(|s| matches!(s, CardShape::Text { font: DisplayFont::Mono, .. }))
            .count();
        assert_eq!(mono_count, 1, "expected exactly one mono Text (the address); got shapes={:?}", shapes);
    }

    #[test]
    fn degraded_lost_variant_headline() {
        let mut p = params(SystemCardKind::Degraded);
        p.variant = Some(DegradedVariant::Lost);
        let shapes = layout_card(&p, PORTRAIT);
        let headline = shapes
            .iter()
            .find_map(|s| match s {
                CardShape::Text { text, font: DisplayFont::Headline, .. } => Some(text.as_str()),
                _ => None,
            })
            .expect("DEGRADED must emit a Headline Text");
        assert!(headline.contains("Lost the wifi"), "got headline={:?}", headline);
    }

    #[test]
    fn degraded_auth_fail_variant_headline() {
        let mut p = params(SystemCardKind::Degraded);
        p.variant = Some(DegradedVariant::AuthFail);
        let shapes = layout_card(&p, PORTRAIT);
        let headline = shapes
            .iter()
            .find_map(|s| match s {
                CardShape::Text { text, font: DisplayFont::Headline, .. } => Some(text.as_str()),
                _ => None,
            })
            .expect("DEGRADED must emit a Headline Text");
        assert!(headline.contains("password"), "got headline={:?}", headline);
    }

    #[test]
    fn degraded_not_found_variant_substitutes_target_ssid() {
        let mut p = params(SystemCardKind::Degraded);
        p.variant = Some(DegradedVariant::NotFoundOr5ghz);
        p.target_ssid = Some("HomeWiFi".to_string());
        let shapes = layout_card(&p, PORTRAIT);
        let headline = shapes
            .iter()
            .find_map(|s| match s {
                CardShape::Text { text, font: DisplayFont::Headline, .. } => Some(text.as_str()),
                _ => None,
            })
            .expect("DEGRADED must emit a Headline Text");
        assert!(headline.contains("HomeWiFi"), "got headline={:?}", headline);
    }

    #[test]
    fn degraded_not_found_and_5ghz_variants_split_copy() {
        // 2026-07-01 (audit 4b follow-up): NotFound (SSID absent
        // from any band) and NotFoundOr5ghz (SSID present but only
        // on 5 GHz) must render distinct sub-line copy so the
        // operator knows whether the router is off / out of range
        // OR the 2.4 GHz radio is disabled.
        let (nf_head, nf_sub) = degraded_copy(Some(DegradedVariant::NotFound), Some("HomeWiFi"));
        let (fg_head, fg_sub) =
            degraded_copy(Some(DegradedVariant::NotFoundOr5ghz), Some("HomeWiFi"));
        assert!(nf_head.contains("HomeWiFi"));
        assert!(fg_head.contains("HomeWiFi"));
        assert_ne!(
            nf_sub, fg_sub,
            "NotFound + NotFoundOr5ghz must render distinct sub-line copy"
        );
        assert!(
            fg_sub.contains("2.4 GHz"),
            "5GHz variant must explicitly name the 2.4 GHz radio; got sub={:?}",
            fg_sub
        );
        assert!(
            !nf_sub.contains("2.4 GHz"),
            "NotFound variant must NOT reference 2.4 GHz (router is off / out of range); got sub={:?}",
            nf_sub
        );
    }

    #[test]
    fn boot_omits_chip_and_shows_centered_mark_portrait() {
        let mut p = params(SystemCardKind::Boot);
        p.address = Some("openmarquee.local".to_string());
        p.ip = Some("192.168.1.47".to_string());
        let shapes = layout_card(&p, PORTRAIT);
        // BOOT has no Chip. (There is no Monogram variant to check for —
        // it was deleted 2026-07-15; its absence is a COMPILE-TIME
        // guarantee. See `all_non_boot_cards_emit_the_mark_image`.)
        assert!(!shapes.iter().any(|s| matches!(s, CardShape::Chip { .. })));
        // The real-artwork mark is present and horizontally centered.
        let img = shapes.iter().find_map(|s| match s {
            CardShape::Image { top_left, height } => Some((*top_left, *height)),
            _ => None,
        });
        assert!(img.is_some(), "BOOT must blit the mark Image");
        let ((ix, iy), h) = img.unwrap();
        // width = h * MARK_ASPECT / aspect; centered => ix ≈ (1-width)/2.
        let w = h * MARK_ASPECT / PORTRAIT;
        assert!((ix - (1.0 - w) / 2.0).abs() < 1e-3, "portrait mark not centered; x={ix}");
        assert!(iy < 0.2, "portrait mark should sit near the top; y={iy}");
        // All shapes stay within the card bounds.
        assert_shapes_in_bounds(&shapes, PORTRAIT);
    }

    #[test]
    fn all_non_boot_cards_emit_the_mark_image() {
        // 2026-07-15 (qarl): every non-BOOT card shows the ONE brand mark
        // — the dot-matrix wordmark (mark.png) via CardShape::Image — NOT
        // an "oM" square. The old CardShape::Monogram was DELETED; its
        // absence is enforced at COMPILE TIME (the variant no longer
        // exists), so this test only needs the POSITIVE assertion that
        // each card kind actually blits the mark Image.
        for kind in [
            SystemCardKind::Setup,
            SystemCardKind::Connecting,
            SystemCardKind::Connected,
            SystemCardKind::Degraded,
        ] {
            let p = params(kind);
            let shapes = layout_card(&p, LANDSCAPE);
            assert!(
                shapes.iter().any(|s| matches!(s, CardShape::Image { .. })),
                "{kind:?} card must blit the mark Image (the wordmark), \
                 not an oM square",
            );
            assert_shapes_in_bounds(&shapes, LANDSCAPE);
        }
    }

    #[test]
    fn boot_landscape_uses_two_columns() {
        // qarl (b): landscape lays the identity out LEFT, the QR RIGHT.
        let mut p = params(SystemCardKind::Boot);
        p.address = Some("http://jasonssign1.local".to_string());
        p.ip = Some("192.168.1.67".to_string());
        p.qr_payload = Some("http://jasonssign1.local".to_string());
        let shapes = layout_card(&p, LANDSCAPE);
        // Mark image is in the LEFT column (x well under 0.5).
        let img_x = shapes.iter().find_map(|s| match s {
            CardShape::Image { top_left, .. } => Some(top_left.0),
            _ => None,
        });
        assert!(img_x.is_some_and(|x| x < 0.2), "landscape mark should be left; x={img_x:?}");
        // URL host sits in the left column. Redesign (2026-07-16): the URL
        // is TWO runs — a muted `http://` right-aligned at the split and
        // the amber host left-aligned at the same split — so assert the
        // host run, not a single left-aligned address.
        let url_left = shapes.iter().any(|s| matches!(s,
            CardShape::Text { text, align: Align::Left, anchor, .. }
                if text.contains("jasonssign1") && anchor.0 < 0.35));
        assert!(url_left, "landscape URL host should be in the left column");
        // QR panel is in the RIGHT half.
        let qr_x = shapes.iter().find_map(|s| match s {
            CardShape::QrPanel { top_left, .. } => Some(top_left.0),
            _ => None,
        });
        assert!(qr_x.is_some_and(|x| x > 0.5), "landscape QR should be in the right column; x={qr_x:?}");
        assert_shapes_in_bounds(&shapes, LANDSCAPE);
    }

    #[test]
    fn boot_mark_and_qr_square_correct_and_in_bounds_both_aspects() {
        // The square-corrected QR must stay in bounds on both a portrait
        // panel and a very wide landscape display (the whole point of the
        // aspect-adaptive layout).
        // Includes 1.25 (5:4) + 1.05 — the near-square landscape band where
        // the QR right-column clamp used to overflow the framebuffer.
        for aspect in [PORTRAIT, LANDSCAPE, 0.4, 2.4, 1.05, 1.25, 1.44] {
            let mut p = params(SystemCardKind::Boot);
            p.address = Some("http://a-very-long-sign-name.local".to_string());
            p.ip = Some("192.168.100.200".to_string());
            p.qr_payload = Some("http://a-very-long-sign-name.local".to_string());
            p.ssid = Some("SomeNetwork-5G".to_string());
            let shapes = layout_card(&p, aspect);
            assert_shapes_in_bounds(&shapes, aspect);
        }
    }

    #[test]
    fn boot_with_qr_payload_emits_qr_panel() {
        // boot-identity-card 2026-07-06: the boot card renders a QR of
        // the identity URL when the backend threads a qr_payload.
        let mut p = params(SystemCardKind::Boot);
        p.address = Some("http://jasonssign1.local".to_string());
        p.qr_payload = Some("http://jasonssign1.local".to_string());
        let shapes = layout_card(&p, PORTRAIT);
        let qr = shapes
            .iter()
            .find_map(|s| match s {
                CardShape::QrPanel { payload, .. } => Some(payload.as_str()),
                _ => None,
            })
            .expect("BOOT must emit a QrPanel when qr_payload is set");
        assert_eq!(qr, "http://jasonssign1.local");
    }

    #[test]
    fn boot_without_qr_payload_omits_qr_panel() {
        // No payload (legacy / fallback caller) -> text-only card, not
        // an empty white QR panel.
        let mut p = params(SystemCardKind::Boot);
        p.address = Some("http://jasonssign1.local".to_string());
        let shapes = layout_card(&p, PORTRAIT);
        assert!(
            !shapes.iter().any(|s| matches!(s, CardShape::QrPanel { .. })),
            "BOOT must NOT emit a QrPanel when qr_payload is absent"
        );
    }

    // ── Boot-card redesign (qarl mockups, 2026-07-16) ──────────────
    // The mockups define the design language: em-dash small-caps
    // labels, a two-colour URL, labelled rows, QR brackets, a divider,
    // taglines + footer. These pin the elements that make it that
    // design rather than the old bare stack.

    #[test]
    fn boot_uses_the_wordmark_image() {
        // CLOSED-DECISION note: both redesign mockups draw an "oM"
        // monogram tile in the lockup. openMarquee has ONE brand mark
        // (the mark.png dot-matrix wordmark), and the `CardShape::
        // Monogram` VARIANT was deleted outright (qarl, 2026-07-15)
        // after the monogram kept recurring — so porting that bit of
        // the mockup is not merely discouraged, it is unrepresentable:
        // there is no shape to emit. That type-level guarantee is
        // stronger than any assertion here, so this test just pins the
        // positive — the card blits the real wordmark on both aspects.
        for aspect in [PORTRAIT, LANDSCAPE] {
            let shapes = layout_card(&params(SystemCardKind::Boot), aspect);
            assert!(
                shapes.iter().any(|s| matches!(s, CardShape::Image { .. })),
                "BOOT card must blit the mark.png wordmark (aspect={aspect})"
            );
        }
    }

    #[test]
    fn boot_url_is_two_coloured_runs_meeting_at_one_split() {
        // The mockup's URL is a muted `http://` + an amber host. Assert
        // BOTH runs exist, in DIFFERENT colours, sharing one anchor so
        // they abut exactly. A single-colour URL (the old design) fails.
        let mut p = params(SystemCardKind::Boot);
        p.address = Some("jasonssign1.local".to_string());
        for aspect in [PORTRAIT, LANDSCAPE] {
            let shapes = layout_card(&p, aspect);
            let scheme = shapes.iter().find_map(|s| match s {
                CardShape::Text { text, color, anchor, align: Align::Right, .. }
                    if text == "http://" => Some((*color, *anchor)),
                _ => None,
            });
            let host = shapes.iter().find_map(|s| match s {
                CardShape::Text { text, color, anchor, align: Align::Left, .. }
                    if text == "jasonssign1.local" => Some((*color, *anchor)),
                _ => None,
            });
            let (scheme_c, scheme_a) = scheme.expect("muted http:// run missing");
            let (host_c, host_a) = host.expect("amber host run missing");
            assert_eq!(scheme_c, MUTED, "http:// must be muted (aspect={aspect})");
            assert_eq!(host_c, ACCENT, "host must be accent (aspect={aspect})");
            assert_ne!(scheme_c, host_c, "URL must be TWO colours (aspect={aspect})");
            assert_eq!(
                scheme_a, host_a,
                "runs must share the split anchor so they abut (aspect={aspect})"
            );
        }
    }

    #[test]
    fn boot_url_shrinks_to_stay_on_card() {
        // Splitting the URL into two runs took it off the paint's
        // single-run fitting, so the layout fits it. Without this a long
        // sign name on a narrow panel pushed the split anchor NEGATIVE
        // (the bounds test caught exactly that at aspect 0.4).
        let long = "a-very-long-sign-name-that-keeps-going.local";
        let fitted = fit_mono_h(7 + long.len(), 0.036, URL_BAND_W_PORTRAIT, PORTRAIT);
        assert!(fitted < 0.036, "a long URL must shrink ({fitted} !< 0.036)");
        // The fitted run actually fits the band.
        let w = mono_run_w(7 + long.len(), fitted, PORTRAIT);
        assert!(w <= URL_BAND_W_PORTRAIT + 1e-4, "fitted URL still overflows: {w}");
        // NON-VACUITY: fit is not just "always shrink" — a genuinely
        // short URL keeps its nominal size (7 + 5 chars at the real
        // portrait aspect is ~0.63 of the band).
        let short = fit_mono_h(7 + 5, 0.036, URL_BAND_W_PORTRAIT, PORTRAIT);
        assert_eq!(short, 0.036, "a short URL must keep its nominal size");
        // And the layout keeps the split anchor ON-CARD for the long
        // name at the narrow aspect that originally went negative.
        let mut p = params(SystemCardKind::Boot);
        p.address = Some(long.to_string());
        assert_shapes_in_bounds(&layout_card(&p, 0.4), 0.4);
    }

    #[test]
    fn boot_has_the_redesign_furniture() {
        // Header label, labelled rows, divider + QR brackets (Rules),
        // and the footer lockup — the elements that distinguish the
        // redesign from the old bare stack.
        let mut p = params(SystemCardKind::Boot);
        p.ip = Some("192.168.1.67".to_string());
        p.ssid = Some("qarl-wifi".to_string());
        p.qr_payload = Some("http://jasonssign1.local".to_string());
        for aspect in [PORTRAIT, LANDSCAPE] {
            let shapes = layout_card(&p, aspect);
            let texts: Vec<&str> = shapes
                .iter()
                .filter_map(|s| match s {
                    CardShape::Text { text, .. } => Some(text.as_str()),
                    _ => None,
                })
                .collect();
            let has = |needle: &str| texts.iter().any(|t| t.contains(needle));
            // Em-dash + small-caps hierarchy.
            assert!(has("—"), "redesign uses em-dash section labels (aspect={aspect})");
            assert!(has("SCAN TO OPEN"), "QR needs its small-caps header (aspect={aspect})");
            assert!(has("IP"), "IP row needs a small-caps label (aspect={aspect})");
            // Rules: the divider + 4 bracket arms (2 per corner L).
            let rules = shapes.iter().filter(|s| matches!(s, CardShape::Rule { .. })).count();
            assert!(rules >= 5, "want divider + 4 bracket arms, got {rules} (aspect={aspect})");
            // Footer lockup.
            assert!(
                has("HANDMADE LED SIGN"),
                "footer lockup missing (aspect={aspect})"
            );
        }
        // Landscape-only copy from the mockup.
        let shapes = layout_card(&p, LANDSCAPE);
        let texts: Vec<&str> = shapes
            .iter()
            .filter_map(|s| match s {
                CardShape::Text { text, .. } => Some(text.as_str()),
                _ => None,
            })
            .collect();
        assert!(
            texts.iter().any(|t| t.contains("REACH THIS SIGN AT")),
            "landscape needs the REACH THIS SIGN AT label"
        );
        assert!(
            texts.iter().any(|t| t.contains("No app, no cloud, no account.")),
            "landscape needs the tagline promise"
        );
        assert!(
            texts.iter().any(|t| t.contains("in your browser")),
            "landscape needs the QR subtitle"
        );
    }

    #[test]
    fn boot_with_ssid_emits_wifi_line() {
        // boot-card 2026-07-07: the connected SSID must appear so a viewer
        // knows which network to join to reach the URL. The INVARIANT is
        // unchanged; the redesign (2026-07-16) changed the FORM from a
        // "Wi-Fi: NEBULA" prefix line to a small-caps "WI-FI" side-label
        // above/beside the bare SSID value. Assert both halves so a
        // dropped label or a dropped value both fail.
        let mut p = params(SystemCardKind::Boot);
        p.ssid = Some("NEBULA".to_string());
        for aspect in [PORTRAIT, LANDSCAPE] {
            let shapes = layout_card(&p, aspect);
            assert!(
                shapes
                    .iter()
                    .any(|s| matches!(s, CardShape::Text { text, .. } if text == "NEBULA")),
                "BOOT card must show the connected SSID value (aspect={aspect})"
            );
            assert!(
                shapes
                    .iter()
                    .any(|s| matches!(s, CardShape::Text { text, .. } if text == "WI-FI")),
                "BOOT card must label the SSID row WI-FI (aspect={aspect})"
            );
        }
    }

    #[test]
    fn boot_with_ip_emits_ip_line() {
        // boot-card 2026-07-07: the device's wlan0 IP appears beneath the
        // mDNS URL as a fallback when `.local` doesn't resolve.
        let mut p = params(SystemCardKind::Boot);
        p.ip = Some("192.168.1.67".to_string());
        let shapes = layout_card(&p, PORTRAIT);
        assert!(
            shapes
                .iter()
                .any(|s| matches!(s, CardShape::Text { text, .. } if text == "192.168.1.67")),
            "BOOT card must show the wlan0 IP line when threaded"
        );
    }

    #[test]
    fn boot_without_ssid_omits_wifi_line() {
        let p = params(SystemCardKind::Boot); // ssid None
        let shapes = layout_card(&p, PORTRAIT);
        assert!(
            !shapes
                .iter()
                .any(|s| matches!(s, CardShape::Text { text, .. } if text.starts_with("Wi-Fi:"))),
            "BOOT card must omit the Wi-Fi line when no SSID is threaded"
        );
    }

    #[test]
    fn boot_hint_reserved_empty_by_default() {
        let p = params(SystemCardKind::Boot);
        let shapes = layout_card(&p, PORTRAIT);
        assert!(
            !shapes.iter().any(|s| matches!(s, CardShape::BootHint { .. })),
            "BOOT must NOT emit a BootHint when boot_hint is None (PR4 reserve)"
        );
    }

    #[test]
    fn boot_hint_appears_when_set() {
        let mut p = params(SystemCardKind::Boot);
        p.boot_hint = Some("Restart 2× more for Setup Mode".to_string());
        let shapes = layout_card(&p, PORTRAIT);
        assert!(shapes.iter().any(|s| matches!(s, CardShape::BootHint { text, .. } if text.contains("Restart"))));
    }

    // ============================================================
    // PR3 finish-pass (2026-07-01) — clamp params, forward-compat
    // `Unknown` variants, and Setup fallback behaviour.
    // ============================================================

    #[test]
    fn clamp_params_truncates_long_ssid_to_max() {
        let mut p = params(SystemCardKind::Setup);
        let too_long = "x".repeat(MAX_SSID_LEN + 20);
        p.ssid = Some(too_long);
        let clamped = clamp_params(p);
        assert_eq!(clamped.ssid.as_deref().unwrap().len(), MAX_SSID_LEN);
    }

    #[test]
    fn clamp_params_truncates_long_pin_to_max() {
        let mut p = params(SystemCardKind::Setup);
        p.pin = Some("1".repeat(MAX_PIN_LEN * 3));
        let clamped = clamp_params(p);
        assert_eq!(clamped.pin.as_deref().unwrap().len(), MAX_PIN_LEN);
    }

    #[test]
    fn clamp_params_preserves_full_wpa2_passphrase() {
        // Regression (2026-07-07): the pin is the setup-AP WPA2
        // passphrase (up to 63 chars), not a short numeric PIN. A
        // 63-char password must survive the clamp intact, else the
        // on-glass credential is truncated + unjoinable.
        let mut p = params(SystemCardKind::Setup);
        let passphrase = "p".repeat(63);
        p.pin = Some(passphrase.clone());
        let clamped = clamp_params(p);
        assert_eq!(clamped.pin.as_deref(), Some(passphrase.as_str()));
    }

    #[test]
    fn clamp_params_truncates_long_qr_payload_to_max() {
        let mut p = params(SystemCardKind::Setup);
        p.qr_payload = Some("A".repeat(MAX_QR_PAYLOAD_LEN * 2));
        let clamped = clamp_params(p);
        assert_eq!(
            clamped.qr_payload.as_deref().unwrap().len(),
            MAX_QR_PAYLOAD_LEN
        );
    }

    #[test]
    fn clamp_params_passes_through_short_values_unchanged() {
        let mut p = params(SystemCardKind::Setup);
        p.ssid = Some("openMarquee-Setup".to_string());
        p.pin = Some("4827".to_string());
        let clamped = clamp_params(p);
        assert_eq!(clamped.ssid.as_deref(), Some("openMarquee-Setup"));
        assert_eq!(clamped.pin.as_deref(), Some("4827"));
    }

    #[test]
    fn clamp_params_preserves_utf8_char_boundary() {
        // A UTF-8 multibyte grapheme (2-byte é ×) must not be
        // bisected by the byte-length truncate.
        let mut p = params(SystemCardKind::Setup);
        let mut long = String::from("café ");
        while long.len() < MAX_SSID_LEN + 5 {
            long.push_str("café ");
        }
        p.ssid = Some(long);
        let clamped = clamp_params(p);
        let out = clamped.ssid.as_deref().unwrap();
        assert!(out.len() <= MAX_SSID_LEN);
        // A `truncate` at a bad byte would fail this validation.
        assert!(std::str::from_utf8(out.as_bytes()).is_ok());
    }

    #[test]
    fn unknown_kind_layouts_as_setup_card() {
        // Forward-compat: an `Unknown` from a newer backend should
        // degrade to the SETUP layout (safest visible fallback).
        let mut p = params(SystemCardKind::Unknown);
        p.qr_payload = Some("WIFI:T:WPA;S:x;P:1;;".to_string());
        p.ssid = Some("x".to_string());
        p.pin = Some("1".to_string());
        let shapes = layout_card(&p, PORTRAIT);
        assert!(shapes.iter().any(|s| matches!(s, CardShape::QrPanel { .. })));
        assert!(shapes.iter().any(|s| matches!(s,
            CardShape::Chip { label, .. } if label == "SETUP MODE"
        )));
    }

    #[test]
    fn unknown_degraded_variant_falls_back_to_lost_copy() {
        // Forward-compat: a `Some(DegradedVariant::Unknown)` from a
        // newer backend must not panic + must render the generic
        // "Lost the wifi connection" headline (safest generic copy).
        let (headline, _sub) = degraded_copy(Some(DegradedVariant::Unknown), None);
        assert!(headline.contains("Lost the wifi"));
    }
}
