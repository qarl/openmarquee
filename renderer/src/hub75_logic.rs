//! HUB75-direct backend — pure, host-testable pre-processing for the hzeller
//! `rpi-rgb-led-matrix` transport.
//!
//! Mirrors `colorlight_logic.rs` / `hdmi_logic.rs`: everything that could
//! plausibly be tested on macOS without touching hardware lives here so
//! `cargo test` on the dev box exercises it. The Linux-only GPIO transport
//! (`hub75.rs`) takes the pre-processed `Rgb888Frame` this module produces
//! and hands it to hzeller's `SetImage`.
//!
//! ## What lives here vs. `hub75.rs`
//! - Here: config + validation, colour-order remap, brightness/gamma LUT,
//!   card-native frame preparation. Pure. No `#[cfg]`. Runs on the dev Mac.
//! - There: `RGBMatrix` FFI, GPIO/SCHED_FIFO privileges, actual bit-banging.
//!   Delegated wholesale to hzeller's C++ library (design doc §2, §4).
//!
//! ## Scope humility
//! HUB75 has NO wire-format to conformance-test at this altitude — the
//! serializer surface is much thinner than Colorlight's L2 packet builder.
//! What we CAN pin: geometry validation, colour-order/LUT byte-exactness,
//! and the "does-not-alias-input-snapshot" contract. That's what this module
//! does and what its tests prove. See `docs/hub75-direct-backend-design-2026-07-12.md`
//! §6, §11, §12.

// ── Public config surface ────────────────────────────────────────────────

/// GPIO pinmap preset the hzeller lib uses. MUST match the physical HAT wiring
/// or timing goes wrong (design doc §10.4). Operator-supplied via env; no
/// auto-detection.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HatMapping {
    /// hzeller's "regular" — bare-GPIO wiring (no HAT).
    Regular,
    /// Adafruit RGB Matrix HAT (single output, no PWM tuning).
    AdafruitHat,
    /// Adafruit RGB Matrix Bonnet or HAT with the PWM cut trace bridged
    /// (better timing on multi-chain).
    AdafruitHatPwm,
}

/// Wire byte order for the RGB triples handed to hzeller. LED panels vary;
/// the correct order is a per-panel config verified visually at Phase 1.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ColorOrder {
    Rgb,
    Bgr,
    Grb,
    Gbr,
    Rbg,
    Brg,
}

impl ColorOrder {
    /// Reorder an (r,g,b) triple into the wire byte order.
    #[inline]
    fn apply(self, r: u8, g: u8, b: u8) -> [u8; 3] {
        match self {
            ColorOrder::Rgb => [r, g, b],
            ColorOrder::Bgr => [b, g, r],
            ColorOrder::Grb => [g, r, b],
            ColorOrder::Gbr => [g, b, r],
            ColorOrder::Rbg => [r, b, g],
            ColorOrder::Brg => [b, r, g],
        }
    }
}

/// Errors surfaced by the pre-processing path — never panic on bad input.
/// Colorlight §11.C / QA §8.3 discipline: bad geometry is a returned error,
/// not a runtime crash.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SerializeError {
    /// Framebuffer length ≠ card_width_px · card_height_px · 3.
    DimensionMismatch {
        expected_bytes: usize,
        got_bytes: usize,
    },
    /// A geometry parameter is zero (would divide by zero downstream).
    InvalidGeometry(&'static str),
    /// A config parameter is outside the range this backend supports on the
    /// target hardware. `field` names the parameter; `value` is what was
    /// passed; `limit` is the failing bound (with direction).
    ConfigOutOfRange {
        field: &'static str,
        value: u32,
        limit: &'static str,
    },
    /// Wiring revision mismatch — used by the Phase-1 fixture harness to
    /// prevent a drifted config from silently "passing" against goldens for
    /// different physical cabling (Colorlight §11.C mirror; C.1 discipline).
    WiringRevisionMismatch { config: String, expected: String },
}

/// All hzeller-relevant configuration. Nothing hard-coded — every knob is
/// env-driven at `Hub75Config::from_env` (see `hub75.rs`). Bounds are
/// enforced by `validate()`.
///
/// The Phase-1 fixture harness will pin `wiring_revision` against a blessed
/// meta.json in the same shape Colorlight does; the field is inert without
/// a consumer today, but `check_wiring_revision` is the consumer landed
/// early so the harness slots into pre-built machinery.
#[derive(Clone, Debug)]
pub struct Hub75Config {
    pub hat: HatMapping,
    pub panel_rows: u16, // module height (32 for P8.2)
    pub panel_cols: u16, // module width (32)
    pub chain: u16,      // modules per chain
    pub parallel: u16,   // number of parallel chain outputs
    pub pwm_bits: u8,    // 1..=11 (hzeller max)
    pub pwm_lsb_nsec: u32,
    pub gpio_slowdown: u8,
    pub limit_refresh_hz: u16,
    pub color_order: ColorOrder,
    pub brightness: u8, // 0-100 (hzeller %), NOT 0-255
    pub gamma_lut: Option<[u8; 256]>,
    pub wiring_revision: String,
}

impl Hub75Config {
    /// The 1-chain fallback default the design doc §1 pins as the safest
    /// first-light target: Adafruit HAT, `parallel=1 × chain=3` → 3 modules,
    /// 96 × 32 card-native, pwm_bits=8 (Pi Zero 2 W ceiling).
    pub fn fallback_1chain() -> Self {
        Self {
            hat: HatMapping::AdafruitHat,
            panel_rows: 32,
            panel_cols: 32,
            chain: 3,
            parallel: 1,
            pwm_bits: 8,
            pwm_lsb_nsec: 130,
            gpio_slowdown: 4,
            limit_refresh_hz: 60,
            color_order: ColorOrder::Rgb,
            brightness: 100,
            gamma_lut: None,
            wiring_revision: "unblessed-phase0-default".to_string(),
        }
    }

    /// Card-native pixel width = `chain · panel_cols`.
    #[inline]
    pub fn card_width_px(&self) -> usize {
        self.chain as usize * self.panel_cols as usize
    }

    /// Card-native pixel height = `parallel · panel_rows`.
    #[inline]
    pub fn card_height_px(&self) -> usize {
        self.parallel as usize * self.panel_rows as usize
    }

    /// Rough theoretical refresh Hz: `1e9 / (pwm_bits * chain * panel_rows *
    /// pwm_lsb_nsec)` per hzeller README's back-of-envelope. Ignores
    /// `parallel` because chains are driven concurrently. Ignores
    /// `limit_refresh_hz` (that's the CAP, this is the CEILING).
    ///
    /// Safe on pre-`validate()` input: returns 0 on any zero factor OR on
    /// u64 arithmetic overflow (a caller passing all field-type maxes can
    /// exceed u64::MAX in the product).
    #[inline]
    pub fn theoretical_refresh_hz(&self) -> u32 {
        // pwm_bits * chain * panel_rows * pwm_lsb_nsec = one full BCM-plane
        // sweep in nanoseconds. Guard zero-division AND overflow so a
        // pre-validate inspection can't panic.
        let denom_ns = (self.pwm_bits as u64)
            .checked_mul(self.chain as u64)
            .and_then(|x| x.checked_mul(self.panel_rows as u64))
            .and_then(|x| x.checked_mul(self.pwm_lsb_nsec as u64));
        match denom_ns {
            Some(0) | None => 0,
            Some(d) => (1_000_000_000u64 / d).min(u32::MAX as u64) as u32,
        }
    }

    /// Validate. Pinned bounds (design doc v2 §12):
    ///   - `panel_rows/cols > 0`; both typically 32 (P8.2). Cap 128 (hzeller max).
    ///   - `chain ∈ 1..=64` (hzeller max chain length).
    ///   - `parallel ∈ 1..=6` (hzeller lib max; Adafruit HAT=1, Bonnet=3
    ///     with Active-3 jumper; 4-6 needs adapter boards, out of fallback
    ///     scope but not rejected here).
    ///   - `pwm_bits ∈ 1..=11` (hzeller cap).
    ///   - `pwm_lsb_nsec > 0`; upper bound 3000 (a full 32-row × 8-bit ×
    ///     chain=3 sweep at 3µs LSB is ~2 Hz — anything higher is a config
    ///     typo, not a valid tuning).
    ///   - `gpio_slowdown ≤ 10` (hzeller max; 4 is the Pi Zero 2 W default).
    ///   - `brightness ≤ 100` (hzeller uses percent, not 0-255).
    ///   - `limit_refresh_hz > 0` (0 would mean "cap at 0 Hz" — meaningless).
    ///
    /// The "will it actually flicker" bound (`theoretical_refresh_hz >= 30`)
    /// is a WARN not an ERROR — some tuning configs deliberately trade
    /// refresh for colour depth (design doc §12 discusses this). Callers
    /// inspect it separately if they want.
    pub fn validate(&self) -> Result<(), SerializeError> {
        if self.panel_rows == 0 || self.panel_cols == 0 {
            return Err(SerializeError::InvalidGeometry("panel dims"));
        }
        if self.chain == 0 || self.parallel == 0 {
            return Err(SerializeError::InvalidGeometry("chain/parallel"));
        }
        if self.pwm_lsb_nsec == 0 {
            return Err(SerializeError::InvalidGeometry("pwm_lsb_nsec"));
        }
        if self.limit_refresh_hz == 0 {
            return Err(SerializeError::InvalidGeometry("limit_refresh_hz"));
        }
        if self.panel_rows > 128 {
            return Err(SerializeError::ConfigOutOfRange {
                field: "panel_rows",
                value: self.panel_rows as u32,
                limit: "<= 128 (hzeller cap)",
            });
        }
        if self.panel_cols > 128 {
            return Err(SerializeError::ConfigOutOfRange {
                field: "panel_cols",
                value: self.panel_cols as u32,
                limit: "<= 128 (hzeller cap)",
            });
        }
        if self.chain > 64 {
            return Err(SerializeError::ConfigOutOfRange {
                field: "chain",
                value: self.chain as u32,
                limit: "<= 64 (hzeller max chain length)",
            });
        }
        if self.parallel > 6 {
            return Err(SerializeError::ConfigOutOfRange {
                field: "parallel",
                value: self.parallel as u32,
                limit: "<= 6 (hzeller lib max)",
            });
        }
        if self.pwm_bits == 0 || self.pwm_bits > 11 {
            return Err(SerializeError::ConfigOutOfRange {
                field: "pwm_bits",
                value: self.pwm_bits as u32,
                limit: "1..=11 (hzeller cap)",
            });
        }
        if self.pwm_lsb_nsec > 3000 {
            return Err(SerializeError::ConfigOutOfRange {
                field: "pwm_lsb_nsec",
                value: self.pwm_lsb_nsec,
                limit: "<= 3000 (typo guard; higher = <2Hz refresh)",
            });
        }
        if self.gpio_slowdown > 10 {
            return Err(SerializeError::ConfigOutOfRange {
                field: "gpio_slowdown",
                value: self.gpio_slowdown as u32,
                limit: "<= 10 (hzeller cap)",
            });
        }
        if self.brightness > 100 {
            return Err(SerializeError::ConfigOutOfRange {
                field: "brightness",
                value: self.brightness as u32,
                limit: "<= 100 (hzeller uses percent)",
            });
        }
        Ok(())
    }

    /// Assert this config's wiring revision matches a blessed fixture's.
    /// Mirrors `ColorlightConfig::check_wiring_revision` — Phase-1 harness
    /// calls this before comparing goldens so a drifted physical wiring
    /// FAILS LOUDLY instead of "passing" against the wrong reference.
    pub fn check_wiring_revision(&self, expected: &str) -> Result<(), SerializeError> {
        if self.wiring_revision != expected {
            return Err(SerializeError::WiringRevisionMismatch {
                config: self.wiring_revision.clone(),
                expected: expected.to_string(),
            });
        }
        Ok(())
    }

    /// Build a config from `OPENMARQUEE_HUB75_*` env vars.
    ///
    /// Every field is optional; unset falls back to the
    /// `fallback_1chain` default. **A set-but-unparseable value
    /// returns `Err` instead of silently defaulting** — operator
    /// typos should surface at arm construction, not blur into
    /// "why isn't my brightness setting taking effect?" hunts.
    ///
    /// **Design-doc v2 HW-assertion pin (§12 addendum):** the
    /// defaults here (`PWM_LSB_NSEC=130`, `GPIO_SLOWDOWN=4`,
    /// `pwm_bits=8`, `limit_refresh_hz=60`, brightness=100) are the
    /// hzeller README recommendations for the Pi Zero 2 W + Adafruit
    /// HAT target. Phase 1 verifies each on real HW; deviations get
    /// tuned via env, not by editing the defaults here (so the
    /// fallback stays predictable for future operators).
    ///
    /// **Not env-configurable at Phase 0:** `wiring_revision`
    /// (comes from the physical bring-up fixture at Phase 1, not
    /// operator config) and `gamma_lut` (needs a file / calibration
    /// asset; deferred to Phase 1 or beyond).
    pub fn from_env() -> Result<Self, ConfigParseError> {
        let mut cfg = Self::fallback_1chain();
        if let Some(v) = env_var("OPENMARQUEE_HUB75_HAT")? {
            cfg.hat = match v.as_str() {
                "regular" => HatMapping::Regular,
                "adafruit-hat" => HatMapping::AdafruitHat,
                "adafruit-hat-pwm" => HatMapping::AdafruitHatPwm,
                other => {
                    return Err(ConfigParseError {
                        key: "OPENMARQUEE_HUB75_HAT",
                        value: other.to_string(),
                        expected: "regular | adafruit-hat | adafruit-hat-pwm",
                    });
                }
            };
        }
        if let Some(v) = parse_env::<u16>("OPENMARQUEE_HUB75_PANEL_ROWS")? {
            cfg.panel_rows = v;
        }
        if let Some(v) = parse_env::<u16>("OPENMARQUEE_HUB75_PANEL_COLS")? {
            cfg.panel_cols = v;
        }
        if let Some(v) = parse_env::<u16>("OPENMARQUEE_HUB75_CHAIN")? {
            cfg.chain = v;
        }
        if let Some(v) = parse_env::<u16>("OPENMARQUEE_HUB75_PARALLEL")? {
            cfg.parallel = v;
        }
        if let Some(v) = parse_env::<u8>("OPENMARQUEE_HUB75_PWM_BITS")? {
            cfg.pwm_bits = v;
        }
        if let Some(v) = parse_env::<u32>("OPENMARQUEE_HUB75_PWM_LSB_NSEC")? {
            cfg.pwm_lsb_nsec = v;
        }
        if let Some(v) = parse_env::<u8>("OPENMARQUEE_HUB75_GPIO_SLOWDOWN")? {
            cfg.gpio_slowdown = v;
        }
        if let Some(v) = parse_env::<u16>("OPENMARQUEE_HUB75_LIMIT_REFRESH_HZ")? {
            cfg.limit_refresh_hz = v;
        }
        if let Some(v) = env_var("OPENMARQUEE_HUB75_COLOR_ORDER")? {
            cfg.color_order = match v.as_str() {
                "rgb" => ColorOrder::Rgb,
                "bgr" => ColorOrder::Bgr,
                "grb" => ColorOrder::Grb,
                "gbr" => ColorOrder::Gbr,
                "rbg" => ColorOrder::Rbg,
                "brg" => ColorOrder::Brg,
                other => {
                    return Err(ConfigParseError {
                        key: "OPENMARQUEE_HUB75_COLOR_ORDER",
                        value: other.to_string(),
                        expected: "rgb | bgr | grb | gbr | rbg | brg",
                    });
                }
            };
        }
        if let Some(v) = parse_env::<u8>("OPENMARQUEE_HUB75_BRIGHTNESS")? {
            cfg.brightness = v;
        }
        Ok(cfg)
    }
}

/// Parse error from `Hub75Config::from_env`. Set-but-bad values are
/// surfaced typed so the arm can render a clear operator message.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigParseError {
    pub key: &'static str,
    pub value: String,
    pub expected: &'static str,
}

impl std::fmt::Display for ConfigParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{}={:?} could not be parsed (expected {})",
            self.key, self.value, self.expected
        )
    }
}

impl std::error::Error for ConfigParseError {}

/// Read an env var, returning `Ok(None)` if unset (not an error).
fn env_var(key: &'static str) -> Result<Option<String>, ConfigParseError> {
    match std::env::var(key) {
        Ok(v) => Ok(Some(v)),
        Err(std::env::VarError::NotPresent) => Ok(None),
        // NotUnicode is an operator-config bug — surface it.
        Err(std::env::VarError::NotUnicode(_)) => Err(ConfigParseError {
            key,
            value: "<non-utf8>".to_string(),
            expected: "utf-8 string",
        }),
    }
}

/// Read an env var and parse to `T`. Unset → `Ok(None)`; set-but-
/// unparseable → typed error (NOT silent default).
fn parse_env<T: std::str::FromStr>(key: &'static str) -> Result<Option<T>, ConfigParseError> {
    match env_var(key)? {
        None => Ok(None),
        Some(s) => match s.parse::<T>() {
            Ok(v) => Ok(Some(v)),
            Err(_) => Err(ConfigParseError {
                key,
                value: s,
                expected: std::any::type_name::<T>(),
            }),
        },
    }
}

// ── Frame preparation ────────────────────────────────────────────────────

/// Prepare one card-native RGB888 canvas for hzeller's `SetImage`.
///
/// - `fb` MUST be exactly `card_width_px · card_height_px · 3` bytes,
///   row-major, R,G,B. The caller (compositor / FBO readback) is
///   responsible for producing a card-native-sized buffer; letterbox /
///   crop of a larger display canvas is upstream (design doc §5).
/// - Applies gamma/brightness LUT (if configured) and colour-order remap.
/// - Returns a fresh `Vec<u8>` — NEVER mutates or aliases `fb`. The
///   integration is responsible for passing a stable snapshot (mirrors
///   Colorlight §11.C `serializer_does_not_alias_input_snapshot`).
///
/// The return value is byte-identical to what hzeller expects (RGB888,
/// row-major, at card-native dims) after colour-order.
pub fn serialize_frame(fb: &[u8], cfg: &Hub75Config) -> Result<Vec<u8>, SerializeError> {
    cfg.validate()?;
    let w = cfg.card_width_px();
    let h = cfg.card_height_px();
    let expected = w * h * 3;
    if fb.len() != expected {
        return Err(SerializeError::DimensionMismatch {
            expected_bytes: expected,
            got_bytes: fb.len(),
        });
    }
    let mut out = Vec::with_capacity(expected);
    for chunk in fb.chunks_exact(3) {
        let r = lut(cfg, chunk[0]);
        let g = lut(cfg, chunk[1]);
        let b = lut(cfg, chunk[2]);
        out.extend_from_slice(&cfg.color_order.apply(r, g, b));
    }
    Ok(out)
}

#[inline]
fn lut(cfg: &Hub75Config, v: u8) -> u8 {
    match &cfg.gamma_lut {
        Some(t) => t[v as usize],
        None => v,
    }
}

// ── Tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
fn unapply_color_order(order: ColorOrder, a: u8, b: u8, c: u8) -> (u8, u8, u8) {
    match order {
        ColorOrder::Rgb => (a, b, c),
        ColorOrder::Bgr => (c, b, a),
        ColorOrder::Grb => (b, a, c),
        ColorOrder::Gbr => (c, a, b),
        ColorOrder::Rbg => (a, c, b),
        ColorOrder::Brg => (b, c, a),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn solid(cfg: &Hub75Config, r: u8, g: u8, b: u8) -> Vec<u8> {
        let n = cfg.card_width_px() * cfg.card_height_px();
        let mut v = Vec::with_capacity(n * 3);
        for _ in 0..n {
            v.extend_from_slice(&[r, g, b]);
        }
        v
    }

    /// Position-dependent framebuffer: every pixel is a distinct function of
    /// its index. Solid fills mask slice/order bugs that a position-varying
    /// input exposes (Colorlight review catch #2; QA discipline).
    fn positional(cfg: &Hub75Config) -> Vec<u8> {
        let n = cfg.card_width_px() * cfg.card_height_px();
        let mut v = Vec::with_capacity(n * 3);
        for k in 0..n {
            v.extend_from_slice(&[
                (k & 0xff) as u8,
                ((k >> 8) & 0xff) as u8,
                ((k >> 16) & 0xff) as u8,
            ]);
        }
        v
    }

    // ── Config validation ────────────────────────────────────────────────

    #[test]
    fn fallback_1chain_validates() {
        assert!(Hub75Config::fallback_1chain().validate().is_ok());
    }

    #[test]
    fn zero_panel_dims_are_invalid_geometry() {
        let mut cfg = Hub75Config::fallback_1chain();
        cfg.panel_cols = 0;
        assert!(matches!(
            cfg.validate(),
            Err(SerializeError::InvalidGeometry(_))
        ));
    }

    #[test]
    fn zero_chain_or_parallel_is_invalid_geometry() {
        let mut cfg = Hub75Config::fallback_1chain();
        cfg.chain = 0;
        assert!(matches!(
            cfg.validate(),
            Err(SerializeError::InvalidGeometry(_))
        ));
        let mut cfg = Hub75Config::fallback_1chain();
        cfg.parallel = 0;
        assert!(matches!(
            cfg.validate(),
            Err(SerializeError::InvalidGeometry(_))
        ));
    }

    #[test]
    fn pwm_bits_ceiling_is_11() {
        let mut cfg = Hub75Config::fallback_1chain();
        cfg.pwm_bits = 12;
        assert!(matches!(
            cfg.validate(),
            Err(SerializeError::ConfigOutOfRange { field: "pwm_bits", .. })
        ));
        cfg.pwm_bits = 0;
        assert!(matches!(
            cfg.validate(),
            Err(SerializeError::ConfigOutOfRange { field: "pwm_bits", .. })
        ));
    }

    #[test]
    fn chain_ceiling_is_64() {
        let mut cfg = Hub75Config::fallback_1chain();
        cfg.chain = 65;
        assert!(matches!(
            cfg.validate(),
            Err(SerializeError::ConfigOutOfRange { field: "chain", .. })
        ));
    }

    #[test]
    fn parallel_ceiling_is_6() {
        let mut cfg = Hub75Config::fallback_1chain();
        cfg.parallel = 7;
        assert!(matches!(
            cfg.validate(),
            Err(SerializeError::ConfigOutOfRange { field: "parallel", .. })
        ));
    }

    #[test]
    fn brightness_ceiling_is_100_percent_not_255() {
        // Explicit guard against the "0-255 vs 0-100" confusion — hzeller uses
        // percent; the design pins that in a test so a future dev who reflexively
        // types 255 gets a loud error.
        let mut cfg = Hub75Config::fallback_1chain();
        cfg.brightness = 101;
        assert!(matches!(
            cfg.validate(),
            Err(SerializeError::ConfigOutOfRange { field: "brightness", .. })
        ));
        cfg.brightness = 100;
        assert!(cfg.validate().is_ok());
    }

    #[test]
    fn pwm_lsb_nsec_typo_guard() {
        let mut cfg = Hub75Config::fallback_1chain();
        cfg.pwm_lsb_nsec = 3001;
        assert!(matches!(
            cfg.validate(),
            Err(SerializeError::ConfigOutOfRange { field: "pwm_lsb_nsec", .. })
        ));
        cfg.pwm_lsb_nsec = 0;
        assert!(matches!(
            cfg.validate(),
            Err(SerializeError::InvalidGeometry(_))
        ));
    }

    #[test]
    fn theoretical_refresh_hz_is_sensible_for_fallback() {
        // Fallback config (pwm_bits=8, chain=3, panel_rows=32, lsb=130) ⇒
        // 1e9 / (8·3·32·130) = ~100 kHz-per-plane sweep basis → ~10k Hz refresh.
        // We're not pinning an exact number (it's a rough ceiling), just that
        // it's above the 30 Hz spec target for the fallback config.
        let hz = Hub75Config::fallback_1chain().theoretical_refresh_hz();
        assert!(hz >= 30, "fallback refresh ceiling {hz} < 30 Hz");
    }

    #[test]
    fn theoretical_refresh_hz_never_divides_by_zero() {
        // If pwm_lsb_nsec is 0, `theoretical_refresh_hz` must return 0 not
        // panic — validate() rejects the config separately; this proves the
        // arithmetic path is safe if a caller inspects it pre-validate.
        let mut cfg = Hub75Config::fallback_1chain();
        cfg.pwm_lsb_nsec = 0;
        assert_eq!(cfg.theoretical_refresh_hz(), 0);
    }

    #[test]
    fn check_wiring_revision_gates_on_mismatch() {
        let cfg = Hub75Config::fallback_1chain();
        assert!(cfg
            .check_wiring_revision("unblessed-phase0-default")
            .is_ok());
        assert!(matches!(
            cfg.check_wiring_revision("thinksign-blessed-2026-07-20"),
            Err(SerializeError::WiringRevisionMismatch { .. })
        ));
    }

    // ── Frame serialization ──────────────────────────────────────────────

    #[test]
    fn serialize_frame_dimension_mismatch_is_an_error_not_a_panic() {
        let cfg = Hub75Config::fallback_1chain();
        let too_small = vec![0u8; 10];
        assert!(matches!(
            serialize_frame(&too_small, &cfg),
            Err(SerializeError::DimensionMismatch { .. })
        ));
    }

    #[test]
    fn serialize_frame_returns_card_native_bytes() {
        // Fallback = 96×32 = 3072 px = 9216 bytes.
        let cfg = Hub75Config::fallback_1chain();
        let fb = solid(&cfg, 10, 20, 30);
        let out = serialize_frame(&fb, &cfg).unwrap();
        assert_eq!(out.len(), 96 * 32 * 3);
        // RGB order default → out is identical to fb.
        assert_eq!(out, fb);
    }

    #[test]
    fn serialize_frame_applies_color_order() {
        let mut cfg = Hub75Config::fallback_1chain();
        cfg.color_order = ColorOrder::Bgr;
        let fb = solid(&cfg, 255, 0, 0);
        let out = serialize_frame(&fb, &cfg).unwrap();
        // Pure-R input under BGR → R lands in byte 3 of every triple.
        assert_eq!(&out[..3], &[0, 0, 255]);
        // Every triple.
        for triple in out.chunks_exact(3) {
            assert_eq!(triple, &[0, 0, 255]);
        }
    }

    #[test]
    fn serialize_frame_applies_lut() {
        // Build an UNMISTAKABLE LUT — not a formula — so a byte-exact assert
        // proves a real lookup happened (Colorlight review catch #1).
        let mut cfg = Hub75Config::fallback_1chain();
        let mut t: [u8; 256] = core::array::from_fn(|i| i as u8);
        t[10] = 222;
        t[20] = 13;
        t[30] = 99;
        cfg.gamma_lut = Some(t);
        let fb = solid(&cfg, 10, 20, 30);
        let out = serialize_frame(&fb, &cfg).unwrap();
        for triple in out.chunks_exact(3) {
            assert_eq!(triple, &[222, 13, 99], "each channel routed through LUT");
        }
    }

    #[test]
    fn all_six_color_orders_are_exact_inverses() {
        // apply() and unapply_color_order() are separate hand-written 6-arm
        // tables — pin every order as an exact inverse so a future edit to
        // one table can't silently break decode paths (Colorlight review
        // catch #3 mirror).
        let samples = [(10u8, 200, 30), (0, 0, 0), (255, 128, 1), (7, 7, 7)];
        for order in [
            ColorOrder::Rgb,
            ColorOrder::Bgr,
            ColorOrder::Grb,
            ColorOrder::Gbr,
            ColorOrder::Rbg,
            ColorOrder::Brg,
        ] {
            for &(r, g, b) in &samples {
                let w = order.apply(r, g, b);
                assert_eq!(
                    unapply_color_order(order, w[0], w[1], w[2]),
                    (r, g, b),
                    "{order:?} apply→unapply not identity"
                );
            }
        }
    }

    #[test]
    fn serialize_frame_endpoints_are_byte_exact() {
        // 0/255 endpoints land exactly (no stray clamp/scale) — LUT-endpoint
        // guard, Colorlight A.3 mirror.
        let cfg = Hub75Config::fallback_1chain();
        let white = serialize_frame(&solid(&cfg, 255, 255, 255), &cfg).unwrap();
        assert!(white.chunks_exact(3).all(|t| t == [0xFF, 0xFF, 0xFF]));
        let black = serialize_frame(&solid(&cfg, 0, 0, 0), &cfg).unwrap();
        assert!(black.chunks_exact(3).all(|t| t == [0, 0, 0]));
    }

    // ── Position-dependent multi-module ─────────────────────────────────

    #[test]
    fn positional_pattern_with_lut_and_bgr_pins_every_pixel() {
        // Combined stress: positional input + non-identity LUT + BGR order.
        // A slice off-by-one, a per-Nth-pixel LUT skip, or a color-order
        // swap that only fires on the first module — any of them show up as
        // a wrong byte at a specific pixel. A solid fill would hide all three.
        let cfg_base = Hub75Config::fallback_1chain(); // 96×32
        let mut cfg = cfg_base.clone();
        let mut t: [u8; 256] = core::array::from_fn(|i| i as u8);
        t[10] = 111;
        t[42] = 222;
        cfg.gamma_lut = Some(t);
        cfg.color_order = ColorOrder::Bgr;
        let fb = positional(&cfg);
        let out = serialize_frame(&fb, &cfg).unwrap();
        // For every input pixel: LUT applied per channel, then BGR swap.
        for (i, (inp, outp)) in fb.chunks_exact(3).zip(out.chunks_exact(3)).enumerate() {
            let (r, g, b) = (t[inp[0] as usize], t[inp[1] as usize], t[inp[2] as usize]);
            assert_eq!(
                outp,
                &[b, g, r],
                "pixel {i}: LUT+BGR pipeline mismatch (inp={inp:?})"
            );
        }
    }

    #[test]
    fn positional_multi_parallel_covers_every_row() {
        // 2 parallel chains × 3 modules × 32 rows = 96w × 64h card.
        // A row-major bug (e.g. only touching the first parallel chain's
        // half of the buffer) surfaces here because the two halves have
        // distinct positional bytes.
        let mut cfg = Hub75Config::fallback_1chain();
        cfg.parallel = 2;
        assert_eq!(cfg.card_height_px(), 64);
        let fb = positional(&cfg);
        let out = serialize_frame(&fb, &cfg).unwrap();
        assert_eq!(out, fb);
        // Spot the split-row boundary — first byte of the second parallel's
        // top row (row 32, col 0).
        let split_start = 32 * cfg.card_width_px() * 3;
        assert_eq!(&out[split_start..split_start + 3], &fb[split_start..split_start + 3]);
    }

    // ── Aliasing contract (Colorlight §11.C mirror) ─────────────────────

    #[test]
    fn serialize_frame_does_not_mutate_input() {
        // The pre-processor takes `&fb` and must never mutate it — the caller
        // owns the snapshot lifecycle. Prove `fb` bytes at multiple positions
        // (each pinned to a specific channel offset within its triple) are
        // untouched after a full serialize pass.
        let cfg = Hub75Config::fallback_1chain();
        let fb = solid(&cfg, 5, 6, 7);
        let _ = serialize_frame(&fb, &cfg).unwrap();
        // Head triple.
        assert_eq!(&fb[0..3], &[5, 6, 7], "head triple untouched");
        // A middle triple, aligned to a 3-byte boundary.
        let mid = (fb.len() / 6) * 3;
        assert_eq!(&fb[mid..mid + 3], &[5, 6, 7], "middle triple untouched");
        // Tail triple.
        let tail = fb.len() - 3;
        assert_eq!(&fb[tail..tail + 3], &[5, 6, 7], "tail triple untouched");
    }

    #[test]
    fn serialize_frame_reads_input_at_call_time() {
        // Prove no cached/aliased reference: mutate fb IN PLACE between two
        // calls and the second output must reflect the new bytes. If the
        // implementation ever cached a `&'static` view or dedupe'd equal
        // inputs, this would fail.
        let cfg = Hub75Config::fallback_1chain();
        let mut fb = solid(&cfg, 5, 6, 7);
        let a = serialize_frame(&fb, &cfg).unwrap();
        fb[0] = 200;
        fb[1] = 201;
        fb[2] = 202;
        let b = serialize_frame(&fb, &cfg).unwrap();
        assert_ne!(a, b, "second call must read the mutated bytes");
        assert_eq!(
            &b[0..3],
            &[200, 201, 202],
            "mutation visible in output at expected byte offset"
        );
    }

    // ── Edge-accept coverage: the pinned bounds must actually accept the
    //    exact ceiling values, not just reject one-over. Guards against a
    //    silent tightening (e.g. `>= 11` typoed for `> 11`) — Colorlight
    //    review discipline.

    #[test]
    fn upper_edge_configs_validate() {
        let mut cfg = Hub75Config::fallback_1chain();
        cfg.pwm_bits = 11; // exact ceiling
        assert!(cfg.validate().is_ok(), "pwm_bits=11 must be accepted");
        cfg.chain = 64;
        assert!(cfg.validate().is_ok(), "chain=64 must be accepted");
        cfg.parallel = 6;
        assert!(cfg.validate().is_ok(), "parallel=6 must be accepted");
        cfg.gpio_slowdown = 10;
        assert!(cfg.validate().is_ok(), "gpio_slowdown=10 must be accepted");
        cfg.panel_rows = 128;
        cfg.panel_cols = 128;
        assert!(
            cfg.validate().is_ok(),
            "panel_rows/cols=128 must be accepted"
        );
        cfg.pwm_lsb_nsec = 3000;
        assert!(
            cfg.validate().is_ok(),
            "pwm_lsb_nsec=3000 must be accepted"
        );
    }

    #[test]
    fn gpio_slowdown_ceiling_rejects_over_10() {
        let mut cfg = Hub75Config::fallback_1chain();
        cfg.gpio_slowdown = 11;
        assert!(matches!(
            cfg.validate(),
            Err(SerializeError::ConfigOutOfRange { field: "gpio_slowdown", .. })
        ));
    }

    #[test]
    fn zero_limit_refresh_hz_is_invalid_geometry() {
        let mut cfg = Hub75Config::fallback_1chain();
        cfg.limit_refresh_hz = 0;
        assert!(matches!(
            cfg.validate(),
            Err(SerializeError::InvalidGeometry(_))
        ));
    }

    #[test]
    fn oversize_panel_dims_are_rejected() {
        let mut cfg = Hub75Config::fallback_1chain();
        cfg.panel_rows = 129;
        assert!(matches!(
            cfg.validate(),
            Err(SerializeError::ConfigOutOfRange { field: "panel_rows", .. })
        ));
        cfg.panel_rows = 32;
        cfg.panel_cols = 129;
        assert!(matches!(
            cfg.validate(),
            Err(SerializeError::ConfigOutOfRange { field: "panel_cols", .. })
        ));
    }

    // ── from_env / ConfigParseError coverage ─────────────────────────
    //
    // `std::env` is process-global, so these tests all touch the SAME
    // env from multiple threads under `cargo test`'s default parallel
    // runner. Use a single `#[test]` that serializes the mutations
    // itself — that costs one test entry but sidesteps the classic
    // set-env-under-parallel-tests race in a way that a
    // per-test-mutex still can't guarantee across an entire crate's
    // test suite (other modules may touch `OPENMARQUEE_*` too).

    fn scoped_env<F: FnOnce()>(pairs: &[(&str, &str)], f: F) {
        // Snapshot prior values, install restore guard BEFORE mutating
        // env — so a panic mid-set-loop still restores cleanly and any
        // pre-existing shell env (unusual but possible for a dev with
        // OPENMARQUEE_HUB75_* preset) is preserved after the test.
        struct Restore {
            prior: Vec<(String, Option<String>)>,
        }
        impl Drop for Restore {
            fn drop(&mut self) {
                for (k, v) in self.prior.drain(..) {
                    // SAFETY: single-threaded within scoped_env; other env
                    // tests must also route through scoped_env.
                    match v {
                        Some(orig) => unsafe { std::env::set_var(&k, orig) },
                        None => unsafe { std::env::remove_var(&k) },
                    }
                }
            }
        }
        let prior: Vec<(String, Option<String>)> = pairs
            .iter()
            .map(|(k, _)| (k.to_string(), std::env::var(k).ok()))
            .collect();
        let _restore = Restore { prior };
        for (k, v) in pairs {
            // SAFETY: as above — scoped_env callers must not overlap.
            unsafe {
                std::env::set_var(k, v);
            }
        }
        f();
    }

    #[test]
    fn from_env_full_coverage_single_serialized_test() {
        // 1) Unset env → defaults match fallback_1chain.
        scoped_env(&[], || {
            let cfg = Hub75Config::from_env().unwrap();
            let d = Hub75Config::fallback_1chain();
            assert_eq!(cfg.hat, d.hat);
            assert_eq!(cfg.panel_rows, d.panel_rows);
            assert_eq!(cfg.panel_cols, d.panel_cols);
            assert_eq!(cfg.chain, d.chain);
            assert_eq!(cfg.parallel, d.parallel);
            assert_eq!(cfg.pwm_bits, d.pwm_bits);
            assert_eq!(cfg.pwm_lsb_nsec, d.pwm_lsb_nsec);
            assert_eq!(cfg.gpio_slowdown, d.gpio_slowdown);
            assert_eq!(cfg.limit_refresh_hz, d.limit_refresh_hz);
            assert_eq!(cfg.color_order, d.color_order);
            assert_eq!(cfg.brightness, d.brightness);
        });

        // 2) All fields overridden with valid values → each parsed.
        scoped_env(
            &[
                ("OPENMARQUEE_HUB75_HAT", "adafruit-hat-pwm"),
                ("OPENMARQUEE_HUB75_PANEL_ROWS", "64"),
                ("OPENMARQUEE_HUB75_PANEL_COLS", "64"),
                ("OPENMARQUEE_HUB75_CHAIN", "2"),
                ("OPENMARQUEE_HUB75_PARALLEL", "3"),
                ("OPENMARQUEE_HUB75_PWM_BITS", "10"),
                ("OPENMARQUEE_HUB75_PWM_LSB_NSEC", "200"),
                ("OPENMARQUEE_HUB75_GPIO_SLOWDOWN", "2"),
                ("OPENMARQUEE_HUB75_LIMIT_REFRESH_HZ", "120"),
                ("OPENMARQUEE_HUB75_COLOR_ORDER", "bgr"),
                ("OPENMARQUEE_HUB75_BRIGHTNESS", "75"),
            ],
            || {
                let cfg = Hub75Config::from_env().unwrap();
                assert_eq!(cfg.hat, HatMapping::AdafruitHatPwm);
                assert_eq!(cfg.panel_rows, 64);
                assert_eq!(cfg.panel_cols, 64);
                assert_eq!(cfg.chain, 2);
                assert_eq!(cfg.parallel, 3);
                assert_eq!(cfg.pwm_bits, 10);
                assert_eq!(cfg.pwm_lsb_nsec, 200);
                assert_eq!(cfg.gpio_slowdown, 2);
                assert_eq!(cfg.limit_refresh_hz, 120);
                assert_eq!(cfg.color_order, ColorOrder::Bgr);
                assert_eq!(cfg.brightness, 75);
                // Env-overridden config still validates.
                assert!(cfg.validate().is_ok());
            },
        );

        // 3) All six color-order values map correctly.
        for (env, expected) in [
            ("rgb", ColorOrder::Rgb),
            ("bgr", ColorOrder::Bgr),
            ("grb", ColorOrder::Grb),
            ("gbr", ColorOrder::Gbr),
            ("rbg", ColorOrder::Rbg),
            ("brg", ColorOrder::Brg),
        ] {
            scoped_env(&[("OPENMARQUEE_HUB75_COLOR_ORDER", env)], || {
                assert_eq!(
                    Hub75Config::from_env().unwrap().color_order,
                    expected,
                    "color_order={env}"
                );
            });
        }

        // 4) All three HAT values map correctly.
        for (env, expected) in [
            ("regular", HatMapping::Regular),
            ("adafruit-hat", HatMapping::AdafruitHat),
            ("adafruit-hat-pwm", HatMapping::AdafruitHatPwm),
        ] {
            scoped_env(&[("OPENMARQUEE_HUB75_HAT", env)], || {
                assert_eq!(
                    Hub75Config::from_env().unwrap().hat,
                    expected,
                    "hat={env}"
                );
            });
        }

        // 5) Bad HAT value → typed error naming the key + expected set.
        scoped_env(&[("OPENMARQUEE_HUB75_HAT", "bogus")], || {
            let err = Hub75Config::from_env().unwrap_err();
            assert_eq!(err.key, "OPENMARQUEE_HUB75_HAT");
            assert_eq!(err.value, "bogus");
            assert!(err.expected.contains("adafruit-hat"), "expected: {}", err.expected);
        });

        // 6) Bad COLOR_ORDER value → typed error.
        scoped_env(&[("OPENMARQUEE_HUB75_COLOR_ORDER", "xyz")], || {
            let err = Hub75Config::from_env().unwrap_err();
            assert_eq!(err.key, "OPENMARQUEE_HUB75_COLOR_ORDER");
            assert_eq!(err.value, "xyz");
        });

        // 7) Bad integer value → typed error (NOT silent default).
        // This is the "operator typo shouldn't hide" contract.
        scoped_env(&[("OPENMARQUEE_HUB75_CHAIN", "not-a-number")], || {
            let err = Hub75Config::from_env().unwrap_err();
            assert_eq!(err.key, "OPENMARQUEE_HUB75_CHAIN");
            assert_eq!(err.value, "not-a-number");
        });

        // 8) Display impl includes key + value + expected.
        let e = ConfigParseError {
            key: "OPENMARQUEE_HUB75_TEST",
            value: "bogus".to_string(),
            expected: "test-expected",
        };
        let s = format!("{e}");
        assert!(s.contains("OPENMARQUEE_HUB75_TEST"), "{s}");
        assert!(s.contains("bogus"), "{s}");
        assert!(s.contains("test-expected"), "{s}");

        // 9) Out-of-range value parses cleanly (u8 max = 255) but is
        // later rejected by validate() → arm-fill sequence
        // (from_env → validate) surfaces the range violation as
        // ConfigOutOfRange, not ConfigParseError. Pin the boundary.
        scoped_env(&[("OPENMARQUEE_HUB75_BRIGHTNESS", "200")], || {
            let cfg = Hub75Config::from_env().unwrap();
            assert_eq!(cfg.brightness, 200);
            assert!(matches!(
                cfg.validate(),
                Err(SerializeError::ConfigOutOfRange { field: "brightness", .. })
            ));
        });
    }

    #[test]
    fn theoretical_refresh_hz_saturates_on_overflow() {
        // Pre-validate arithmetic guard: an adversarial field-max config
        // would overflow u64 in the multiplication chain. Must return 0
        // (checked_mul chain), not panic in debug / wrap in release.
        let cfg = Hub75Config {
            hat: HatMapping::Regular,
            panel_rows: u16::MAX,
            panel_cols: 32,
            chain: u16::MAX,
            parallel: 1,
            pwm_bits: u8::MAX,
            pwm_lsb_nsec: u32::MAX,
            gpio_slowdown: 4,
            limit_refresh_hz: 60,
            color_order: ColorOrder::Rgb,
            brightness: 100,
            gamma_lut: None,
            wiring_revision: "overflow-fuzz".to_string(),
        };
        assert_eq!(cfg.theoretical_refresh_hz(), 0, "overflow must return 0");
        // And validate() STILL rejects the config so the overflow value can't
        // slip past into flicker-check branches.
        assert!(cfg.validate().is_err());
    }
}
