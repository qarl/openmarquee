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
    /// The brand monogram lockup (amber tile + "openMarquee").
    /// Positioned by `top_left` relative to card; the tile is
    /// square sized `tile_size` (normalized). Word follows the
    /// tile with a small gap.
    Monogram { top_left: (f32, f32), tile_size: f32 },
    /// The state chip in the top-right corner. Solid-fill color +
    /// label.
    Chip {
        top_right: (f32, f32),
        label: String,
        bg: Rgb,
        ink: Rgb,
        text_height: f32,
    },
    /// A line / multi-line block of text. `max_height` is the
    /// glyph cap-height in normalized units (one line). `text`
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

/// PR3 layout entrypoint: turn IPC params into the shape list.
/// Pure — no GL deps, no time deps; tests pin layout positions per
/// card kind.
pub fn layout_card(params: &RenderSystemCardParams) -> Vec<CardShape> {
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
        SystemCardKind::Boot => layout_boot(params, &mut shapes),
    }
    shapes
}

// === Per-kind layouts. All offsets in normalized 0..1 of card
// width/height. cqw -> normalized: divide by 100. ===

/// Monogram offset on every non-BOOT card (top-left padding).
const MONOGRAM_TL: (f32, f32) = (0.046, 0.046);
/// Tile width (4.4cqw).
const MONOGRAM_TILE: f32 = 0.044;
/// Chip top-right offset.
const CHIP_TR: (f32, f32) = (0.046, 0.046);
/// Chip's label cap-height (1.7cqw).
const CHIP_HEIGHT: f32 = 0.017;

fn layout_setup(params: &RenderSystemCardParams, shapes: &mut Vec<CardShape>) {
    shapes.push(CardShape::Monogram {
        top_left: MONOGRAM_TL,
        tile_size: MONOGRAM_TILE,
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
    shapes.push(CardShape::Monogram {
        top_left: MONOGRAM_TL,
        tile_size: MONOGRAM_TILE,
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
    shapes.push(CardShape::Monogram {
        top_left: MONOGRAM_TL,
        tile_size: MONOGRAM_TILE,
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
    shapes.push(CardShape::Monogram {
        top_left: MONOGRAM_TL,
        tile_size: MONOGRAM_TILE,
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

fn layout_boot(params: &RenderSystemCardParams, shapes: &mut Vec<CardShape>) {
    // BOOT identity card (boot-identity-card 2026-07-06): centered
    // monogram + the device's real mDNS URL + a QR of that URL. Text
    // shapes center via x=0.5 + Align::Center; the QR panel centers by
    // anchoring its top-left at 0.5 - size/2.
    shapes.push(CardShape::Monogram {
        top_left: (0.40, 0.14),
        tile_size: MONOGRAM_TILE,
    });
    let address = params.address.as_deref().unwrap_or("openmarquee.local");
    shapes.push(CardShape::Text {
        anchor: (0.50, 0.30),
        max_height: 0.05,
        color: ACCENT,
        font: DisplayFont::Mono,
        align: Align::Center,
        text: address.to_string(),
    });
    let ip = params.ip.as_deref().unwrap_or("");
    if !ip.is_empty() {
        shapes.push(CardShape::Text {
            anchor: (0.50, 0.37),
            max_height: 0.027,
            color: MUTED,
            font: DisplayFont::Mono,
            align: Align::Center,
            text: ip.to_string(),
        });
    }
    // QR of the identity URL. Only emitted when the backend threaded a
    // non-empty qr_payload — a BOOT card fired without one (legacy /
    // fallback callers) keeps the pre-feature text-only layout instead
    // of painting an empty white panel.
    let qr_payload = params.qr_payload.clone().unwrap_or_default();
    if !qr_payload.is_empty() {
        const BOOT_QR_SIZE: f32 = 0.22;
        shapes.push(CardShape::QrPanel {
            top_left: (0.5 - BOOT_QR_SIZE / 2.0, 0.44),
            size: BOOT_QR_SIZE,
            payload: qr_payload,
            caption: "Scan to open".to_string(),
        });
    }
    // Connected Wi-Fi SSID (boot-card 2026-07-07): which network to join
    // to reach the URL above (the mDNS URL only resolves on the sign's
    // own network). Omitted when the backend didn't thread one — not yet
    // connected at boot-card time.
    if let Some(ssid) = params.ssid.as_deref() {
        if !ssid.is_empty() {
            shapes.push(CardShape::Text {
                anchor: (0.50, 0.88),
                max_height: 0.03,
                color: MUTED,
                font: DisplayFont::Mono,
                align: Align::Center,
                text: format!("Wi-Fi: {ssid}"),
            });
        }
    }
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
            let shapes = layout_card(&params(kind));
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
        let shapes = layout_card(&p);
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
        let shapes = layout_card(&p);
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
        let shapes = layout_card(&p);
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
        let shapes = layout_card(&p);
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
        let shapes = layout_card(&p);
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
        let shapes = layout_card(&p);
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
        let shapes = layout_card(&p);
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
        let shapes = layout_card(&p);
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
        let shapes = layout_card(&params(SystemCardKind::Connecting));
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
        let shapes = layout_card(&p);
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
        let shapes = layout_card(&p);
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
        let shapes = layout_card(&p);
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
        let shapes = layout_card(&p);
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
        let shapes = layout_card(&p);
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
    fn boot_omits_chip_and_monogram_centered() {
        let mut p = params(SystemCardKind::Boot);
        p.address = Some("openmarquee.local".to_string());
        p.ip = Some("192.168.1.47".to_string());
        let shapes = layout_card(&p);
        // BOOT has no Chip.
        assert!(!shapes.iter().any(|s| matches!(s, CardShape::Chip { .. })));
        // Monogram is centered (not at top-left).
        let mono = shapes.iter().find_map(|s| match s {
            CardShape::Monogram { top_left, .. } => Some(*top_left),
            _ => None,
        });
        assert!(mono.is_some());
        let (mx, _) = mono.unwrap();
        assert!(mx > 0.3, "BOOT monogram should be horizontally centered; got x={}", mx);
    }

    #[test]
    fn boot_with_qr_payload_emits_qr_panel() {
        // boot-identity-card 2026-07-06: the boot card renders a QR of
        // the identity URL when the backend threads a qr_payload.
        let mut p = params(SystemCardKind::Boot);
        p.address = Some("http://jasonssign1.local".to_string());
        p.qr_payload = Some("http://jasonssign1.local".to_string());
        let shapes = layout_card(&p);
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
        let shapes = layout_card(&p);
        assert!(
            !shapes.iter().any(|s| matches!(s, CardShape::QrPanel { .. })),
            "BOOT must NOT emit a QrPanel when qr_payload is absent"
        );
    }

    #[test]
    fn boot_with_ssid_emits_wifi_line() {
        // boot-card 2026-07-07: the connected SSID appears as a
        // "Wi-Fi: <ssid>" line so a viewer knows which network to join
        // to reach the URL.
        let mut p = params(SystemCardKind::Boot);
        p.ssid = Some("NEBULA".to_string());
        let shapes = layout_card(&p);
        assert!(
            shapes
                .iter()
                .any(|s| matches!(s, CardShape::Text { text, .. } if text == "Wi-Fi: NEBULA")),
            "BOOT card must show the connected Wi-Fi SSID line"
        );
    }

    #[test]
    fn boot_with_ip_emits_ip_line() {
        // boot-card 2026-07-07: the device's wlan0 IP appears beneath the
        // mDNS URL as a fallback when `.local` doesn't resolve.
        let mut p = params(SystemCardKind::Boot);
        p.ip = Some("192.168.1.67".to_string());
        let shapes = layout_card(&p);
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
        let shapes = layout_card(&p);
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
        let shapes = layout_card(&p);
        assert!(
            !shapes.iter().any(|s| matches!(s, CardShape::BootHint { .. })),
            "BOOT must NOT emit a BootHint when boot_hint is None (PR4 reserve)"
        );
    }

    #[test]
    fn boot_hint_appears_when_set() {
        let mut p = params(SystemCardKind::Boot);
        p.boot_hint = Some("Restart 2× more for Setup Mode".to_string());
        let shapes = layout_card(&p);
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
        let shapes = layout_card(&p);
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
