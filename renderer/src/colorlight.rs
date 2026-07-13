//! Colorlight 5A-75B output stage — the glue between the GL frame tap and the wire.
//!
//! Mirrors the `hdmi_logic.rs` / `hdmi.rs` split: everything in THIS file except the
//! Linux-only `RawSocketSink` (added with the OutputMode arm) is pure and runs on the
//! macOS `cargo test` gate. The byte-exact serializer lives in [`colorlight_logic`];
//! this module packs the readback and routes serialized frames to a [`FrameSink`].
//!
//! Per-frame data flow (design doc §5):
//! ```text
//!   GL composite @128x96  →  hdmi::capture_fbo_to_rgba (RGBA8, Y-flipped, owned)
//!                         →  pack_rgba_topdown_to_rgb888 (drop alpha, no re-flip)
//!                         →  colorlight_logic::serialize_frame (L2 frames)
//!                         →  FrameSink::send_frame (AF_PACKET blast / mock)
//! ```
//! The `FrameSink` trait is the seam that makes the whole stage testable without a
//! socket or hardware: the integration test drives a `VecSink` in-process.

use crate::colorlight_logic::{serialize_frame, ColorOrder, ColorlightConfig, SerializeError};

// ── Errors ───────────────────────────────────────────────────────────────────

/// Output-stage failures. Never panics on bad input (mirrors `SerializeError`).
#[derive(Debug)]
pub enum PipelineError {
    /// The RGBA readback wasn't `width*height*4` bytes (FBO/config size mismatch).
    ReadbackSize { expected: usize, got: usize },
    /// The pure serializer rejected the packed RGB888 frame.
    Serialize(SerializeError),
    /// The sink failed to emit the frame (socket error, etc.).
    Sink(std::io::Error),
}

impl std::fmt::Display for PipelineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PipelineError::ReadbackSize { expected, got } => {
                write!(
                    f,
                    "RGBA readback size mismatch: expected {expected} bytes, got {got}"
                )
            }
            PipelineError::Serialize(e) => write!(f, "serialize failed: {e:?}"),
            PipelineError::Sink(e) => write!(f, "frame sink failed: {e}"),
        }
    }
}

impl std::error::Error for PipelineError {}

impl From<SerializeError> for PipelineError {
    fn from(e: SerializeError) -> Self {
        PipelineError::Serialize(e)
    }
}

// ── Frame sink ───────────────────────────────────────────────────────────────

/// Consumes the ordered Layer-2 frames for ONE video frame (brightness `0x0A`, the
/// `0x55` rows, the `0x01` latch). Swapping the transport here — real `AF_PACKET`
/// vs. an in-process mock — is what makes the output stage testable without a NIC or
/// hardware (design doc §6 Layer 2 rig).
pub trait FrameSink {
    /// Emit all of one video frame's L2 packets, in order.
    fn send_frame(&mut self, frames: &[Vec<u8>]) -> std::io::Result<()>;
}

/// Test / dev sink: records every frame's packets in memory. The integration test's
/// "loopback" per admin's ask #5 (in-process mock).
#[derive(Default)]
pub struct VecSink {
    /// One entry per video frame; each is that frame's ordered L2 packets.
    pub frames: Vec<Vec<Vec<u8>>>,
}

impl FrameSink for VecSink {
    fn send_frame(&mut self, frames: &[Vec<u8>]) -> std::io::Result<()> {
        self.frames.push(frames.to_vec());
        Ok(())
    }
}

// ── RGBA → RGB888 packer ─────────────────────────────────────────────────────

/// Pack a **top-down** RGBA8 readback into the RGB888 `width*height*3` buffer
/// [`serialize_frame`] expects, dropping the alpha byte. Writes into `out` (reused
/// across frames to avoid a per-frame alloc on the Pi Zero 2 W hot loop).
///
/// Contract / layering (so the orientation isn't "bit twice", cf. GL-y-up vs
/// canvas-y-down): the input is ALREADY top-down (row 0 = visual top) because
/// `hdmi::capture_fbo_to_rgba` flips GL's bottom-up rows during the readback. This
/// function does **no** Y-flip — the canvas handed to the serializer is "what's on
/// the glass, row 0 = top", and the wiring transpose is `card_to_canvas`'s job. One
/// orientation transform per layer.
pub fn pack_rgba_topdown_to_rgb888(
    rgba: &[u8],
    width: usize,
    height: usize,
    out: &mut Vec<u8>,
) -> Result<(), PipelineError> {
    let expected = width * height * 4;
    if rgba.len() != expected {
        return Err(PipelineError::ReadbackSize {
            expected,
            got: rgba.len(),
        });
    }
    out.clear();
    out.reserve(width * height * 3);
    for px in 0..(width * height) {
        let s = px * 4;
        out.push(rgba[s]);
        out.push(rgba[s + 1]);
        out.push(rgba[s + 2]);
        // rgba[s + 3] (alpha) intentionally dropped.
    }
    Ok(())
}

// ── Output-stage driver ──────────────────────────────────────────────────────

/// Owns the config + sink + a reused RGB scratch buffer. Per frame: pack the RGBA
/// readback → serialize → send. Host-testable end-to-end with a [`VecSink`]; the
/// Linux arm wires a real GL tap + AF_PACKET sink to the exact same driver.
pub struct ColorlightOutput<S: FrameSink> {
    cfg: ColorlightConfig,
    sink: S,
    rgb: Vec<u8>,
    frames_sent: u64,
}

impl<S: FrameSink> ColorlightOutput<S> {
    pub fn new(cfg: ColorlightConfig, sink: S) -> Self {
        let cap = cfg.width as usize * cfg.height as usize * 3;
        Self {
            cfg,
            sink,
            rgb: Vec::with_capacity(cap),
            frames_sent: 0,
        }
    }

    /// Push one **top-down** RGBA8 frame (`width*height*4` bytes) through the
    /// pipeline: pack (drop alpha) → serialize → send.
    ///
    /// Stable-snapshot contract (design doc §5/C.1): `serialize_frame` borrows
    /// `self.rgb` by shared ref and never aliases it; `self.rgb` is freshly packed
    /// from the caller's owned `rgba` (itself a copy out of `capture_fbo_to_rgba`),
    /// so there is no mid-frame-tear window.
    pub fn push_rgba_frame(&mut self, rgba: &[u8]) -> Result<(), PipelineError> {
        let w = self.cfg.width as usize;
        let h = self.cfg.height as usize;
        pack_rgba_topdown_to_rgb888(rgba, w, h, &mut self.rgb)?;
        let frames = serialize_frame(&self.rgb, &self.cfg)?;
        self.sink.send_frame(&frames).map_err(PipelineError::Sink)?;
        self.frames_sent += 1;
        Ok(())
    }

    /// Frames successfully pushed (cadence / smoke instrumentation).
    pub fn frames_sent(&self) -> u64 {
        self.frames_sent
    }

    pub fn config(&self) -> &ColorlightConfig {
        &self.cfg
    }

    /// Borrow the sink (tests inspect a `VecSink`; the arm never needs this).
    pub fn sink(&self) -> &S {
        &self.sink
    }
}

// ── Env config (OPENMARQUEE_COLORLIGHT_*) ─────────────────────────────────────

/// Env-var prefix for all Colorlight config (matches the renderer convention).
const ENV_PREFIX: &str = "OPENMARQUEE_COLORLIGHT_";

/// Read `OPENMARQUEE_COLORLIGHT_*` into a `(ColorlightConfig, iface)` pair.
///
/// `IFACE` is required (the transport needs a NIC); every geometry/color field
/// defaults to the ThinkSIGN target ([`ColorlightConfig::thinksign_default`]) and is
/// overridable — nothing is hard-coded in the arm (design doc §4). The assembled
/// geometry is `validate()`d up-front so a bad config fails at startup, not on the
/// first frame. Returns a human-readable error string on a bad/missing value.
pub fn config_from_env() -> Result<(ColorlightConfig, String), String> {
    config_from_getter(|k| std::env::var(k).ok())
}

/// Testable core of [`config_from_env`]: takes a getter so tests never touch the
/// process environment (which is global + racy under a parallel test runner).
fn config_from_getter(
    get: impl Fn(&str) -> Option<String>,
) -> Result<(ColorlightConfig, String), String> {
    let iface = get(&key("IFACE"))
        .ok_or_else(|| format!("{ENV_PREFIX}IFACE is required (the NIC to blast frames on)"))?;
    if iface.trim().is_empty() {
        return Err(format!("{ENV_PREFIX}IFACE must not be empty"));
    }

    let mut cfg = ColorlightConfig::thinksign_default();
    if let Some(v) = parse_u16(&get, "WIDTH")? {
        cfg.width = v;
    }
    if let Some(v) = parse_u16(&get, "HEIGHT")? {
        cfg.height = v;
    }
    if let Some(v) = parse_u16(&get, "PANEL_W")? {
        cfg.panel_w = v;
    }
    if let Some(v) = parse_u16(&get, "PANEL_H")? {
        cfg.panel_h = v;
    }
    if let Some(v) = parse_u16(&get, "PARALLEL")? {
        cfg.outputs = v;
    }
    if let Some(v) = parse_u16(&get, "CHAIN")? {
        cfg.chain = v;
    }
    if let Some(v) = parse_u8(&get, "BRIGHTNESS")? {
        cfg.brightness = v;
    }
    if let Some(s) = get(&key("COLOR_ORDER")) {
        cfg.color_order = parse_color_order(&s)?;
    }
    if let Some(s) = get(&key("CHAIN_REVERSED")) {
        cfg.chain_reversed = parse_bool(&s);
    }
    if let Some(s) = get(&key("DEST_MAC")) {
        cfg.dest_mac = parse_mac(&s)?;
    }
    if let Some(s) = get(&key("SRC_MAC")) {
        cfg.src_mac = parse_mac(&s)?;
    }
    if let Some(s) = get(&key("WIRING_REVISION")) {
        cfg.wiring_revision = s;
    }

    cfg.validate()
        .map_err(|e| format!("invalid Colorlight geometry from env: {e:?}"))?;
    Ok((cfg, iface.trim().to_string()))
}

fn key(suffix: &str) -> String {
    format!("{ENV_PREFIX}{suffix}")
}

fn parse_u16(get: &impl Fn(&str) -> Option<String>, suffix: &str) -> Result<Option<u16>, String> {
    match get(&key(suffix)) {
        Some(s) => s
            .trim()
            .parse::<u16>()
            .map(Some)
            .map_err(|_| format!("{ENV_PREFIX}{suffix}: '{s}' is not a u16")),
        None => Ok(None),
    }
}

fn parse_u8(get: &impl Fn(&str) -> Option<String>, suffix: &str) -> Result<Option<u8>, String> {
    match get(&key(suffix)) {
        Some(s) => s
            .trim()
            .parse::<u8>()
            .map(Some)
            .map_err(|_| format!("{ENV_PREFIX}{suffix}: '{s}' is not a u8 (0-255)")),
        None => Ok(None),
    }
}

/// Parse a color-order token (case-insensitive): rgb/bgr/grb/gbr/rbg/brg.
fn parse_color_order(s: &str) -> Result<ColorOrder, String> {
    match s.trim().to_ascii_lowercase().as_str() {
        "rgb" => Ok(ColorOrder::Rgb),
        "bgr" => Ok(ColorOrder::Bgr),
        "grb" => Ok(ColorOrder::Grb),
        "gbr" => Ok(ColorOrder::Gbr),
        "rbg" => Ok(ColorOrder::Rbg),
        "brg" => Ok(ColorOrder::Brg),
        other => Err(format!(
            "{ENV_PREFIX}COLOR_ORDER: '{other}' not one of rgb/bgr/grb/gbr/rbg/brg"
        )),
    }
}

/// Truthy env values: 1/true/yes/on (case-insensitive); everything else false.
fn parse_bool(s: &str) -> bool {
    matches!(
        s.trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

/// Parse `aa:bb:cc:dd:ee:ff` (also `-`/`.`-separated) into 6 bytes.
fn parse_mac(s: &str) -> Result<[u8; 6], String> {
    let parts: Vec<&str> = s.trim().split([':', '-', '.']).collect();
    if parts.len() != 6 {
        return Err(format!(
            "MAC '{s}' must be 6 colon/dash-separated hex bytes"
        ));
    }
    let mut mac = [0u8; 6];
    for (i, p) in parts.iter().enumerate() {
        mac[i] =
            u8::from_str_radix(p, 16).map_err(|_| format!("MAC '{s}': '{p}' is not a hex byte"))?;
    }
    Ok(mac)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A deterministic, position-dependent top-down RGBA readback (distinct pixels
    /// so a slice/orientation bug can't hide behind a solid). Alpha set to a
    /// distinctive constant so the drop is observable.
    fn synthetic_rgba(w: usize, h: usize) -> Vec<u8> {
        let mut v = vec![0u8; w * h * 4];
        for px in 0..(w * h) {
            v[px * 4] = (px & 0xff) as u8;
            v[px * 4 + 1] = ((px >> 8) & 0xff) as u8;
            v[px * 4 + 2] = ((px >> 4) & 0xff) as u8;
            v[px * 4 + 3] = 0xA5; // alpha — must not reach the wire
        }
        v
    }

    #[test]
    fn pack_drops_alpha_keeps_rgb_and_does_not_reflip() {
        // 2x2, top-down: row 0 = [p0, p1], row 1 = [p2, p3]. The packer must NOT
        // flip (that was capture_fbo_to_rgba's job) — position is preserved.
        let rgba = vec![
            10, 20, 30, 111, 40, 50, 60, 122, // row 0
            70, 80, 90, 133, 100, 110, 120, 144, // row 1
        ];
        let mut out = Vec::new();
        pack_rgba_topdown_to_rgb888(&rgba, 2, 2, &mut out).unwrap();
        assert_eq!(
            out,
            vec![10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120],
            "alpha dropped, RGB order + row order preserved, no re-flip"
        );
    }

    #[test]
    fn pack_reuses_the_output_buffer() {
        // Second pack into the same Vec must not append to the first.
        let mut out = Vec::new();
        pack_rgba_topdown_to_rgb888(&[1, 2, 3, 4], 1, 1, &mut out).unwrap();
        pack_rgba_topdown_to_rgb888(&[9, 8, 7, 6], 1, 1, &mut out).unwrap();
        assert_eq!(
            out,
            vec![9, 8, 7],
            "buffer cleared + repacked, not appended"
        );
    }

    #[test]
    fn pack_rejects_wrong_size_readback() {
        let mut out = Vec::new();
        assert!(matches!(
            pack_rgba_topdown_to_rgb888(&[0u8; 7], 2, 1, &mut out),
            Err(PipelineError::ReadbackSize {
                expected: 8,
                got: 7
            })
        ));
    }

    /// The integration test (admin ask #5): drive the 128x96 Colorlight output stage
    /// end-to-end through the in-process mock sink and verify serialized frames flow.
    #[test]
    fn output_stage_pushes_serialized_frames_end_to_end() {
        let cfg = ColorlightConfig::thinksign_default(); // 128x96, transpose-valid
        let (w, h) = (cfg.width as usize, cfg.height as usize);
        let rgba = synthetic_rgba(w, h);

        let mut out = ColorlightOutput::new(cfg.clone(), VecSink::default());
        out.push_rgba_frame(&rgba).unwrap();
        out.push_rgba_frame(&rgba).unwrap();

        assert_eq!(out.frames_sent(), 2, "two frames pushed");
        let got = &out.sink().frames;
        assert_eq!(got.len(), 2, "sink received two video frames");

        // Each frame = the exact serialization of the alpha-stripped canvas.
        let mut expected_rgb = Vec::new();
        pack_rgba_topdown_to_rgb888(&rgba, w, h, &mut expected_rgb).unwrap();
        let expected_frames = serialize_frame(&expected_rgb, &cfg).unwrap();
        assert_eq!(
            got[0], expected_frames,
            "sink got exactly the serialized L2 frames"
        );
        assert_eq!(
            got[1], expected_frames,
            "frame 2 identical for a static input"
        );

        // Structurally: brightness + 128 rows + latch.
        assert_eq!(
            got[0].len(),
            1 + cfg.card_rows() + 1,
            "0x0A + 128x 0x55 + 0x01"
        );
    }

    #[test]
    fn output_stage_surfaces_a_readback_size_mismatch() {
        let cfg = ColorlightConfig::thinksign_default();
        let mut out = ColorlightOutput::new(cfg, VecSink::default());
        // One byte short of 128*96*4 — must error, not panic, and not reach the sink.
        let bad = vec![0u8; 128 * 96 * 4 - 1];
        assert!(matches!(
            out.push_rgba_frame(&bad),
            Err(PipelineError::ReadbackSize { .. })
        ));
        assert_eq!(out.frames_sent(), 0);
        assert!(
            out.sink().frames.is_empty(),
            "nothing emitted on a bad frame"
        );
    }

    // ── env config ──

    fn env_get<'a>(pairs: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
        move |k| {
            pairs
                .iter()
                .find(|(key, _)| *key == k)
                .map(|(_, v)| v.to_string())
        }
    }

    #[test]
    fn config_from_getter_defaults_to_thinksign_when_only_iface_set() {
        let (cfg, iface) =
            config_from_getter(env_get(&[("OPENMARQUEE_COLORLIGHT_IFACE", "eth0")])).unwrap();
        assert_eq!(iface, "eth0");
        let d = ColorlightConfig::thinksign_default();
        assert_eq!(
            (
                cfg.width,
                cfg.height,
                cfg.outputs,
                cfg.chain,
                cfg.color_order
            ),
            (d.width, d.height, d.outputs, d.chain, d.color_order)
        );
    }

    #[test]
    fn config_from_getter_requires_iface() {
        let err = config_from_getter(env_get(&[])).unwrap_err();
        assert!(
            err.contains("IFACE"),
            "missing IFACE must be named in the error"
        );
        assert!(config_from_getter(env_get(&[("OPENMARQUEE_COLORLIGHT_IFACE", "  ")])).is_err());
    }

    #[test]
    fn config_from_getter_applies_overrides() {
        let (cfg, iface) = config_from_getter(env_get(&[
            ("OPENMARQUEE_COLORLIGHT_IFACE", "eth1"),
            ("OPENMARQUEE_COLORLIGHT_BRIGHTNESS", "128"),
            ("OPENMARQUEE_COLORLIGHT_COLOR_ORDER", "BGR"),
            ("OPENMARQUEE_COLORLIGHT_CHAIN_REVERSED", "yes"),
            ("OPENMARQUEE_COLORLIGHT_DEST_MAC", "aa:bb:cc:dd:ee:ff"),
            (
                "OPENMARQUEE_COLORLIGHT_WIRING_REVISION",
                "thinksign-blessed-2026-07-20",
            ),
        ]))
        .unwrap();
        assert_eq!(iface, "eth1");
        assert_eq!(cfg.brightness, 128);
        assert_eq!(cfg.color_order, ColorOrder::Bgr);
        assert!(cfg.chain_reversed);
        assert_eq!(cfg.dest_mac, [0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff]);
        assert_eq!(cfg.wiring_revision, "thinksign-blessed-2026-07-20");
    }

    #[test]
    fn config_from_getter_rejects_unparseable_value() {
        let err = config_from_getter(env_get(&[
            ("OPENMARQUEE_COLORLIGHT_IFACE", "eth0"),
            ("OPENMARQUEE_COLORLIGHT_BRIGHTNESS", "over9000"),
        ]))
        .unwrap_err();
        assert!(err.contains("BRIGHTNESS"));
    }

    #[test]
    fn config_from_getter_rejects_geometry_that_breaks_the_transpose() {
        // chain=2 → row_width_px = 2*32 = 64 ≠ height 96 → validate() fails at startup.
        let r = config_from_getter(env_get(&[
            ("OPENMARQUEE_COLORLIGHT_IFACE", "eth0"),
            ("OPENMARQUEE_COLORLIGHT_CHAIN", "2"),
        ]));
        assert!(
            r.is_err(),
            "env geometry violating the remap must fail at startup"
        );
    }

    #[test]
    fn parse_mac_accepts_valid_and_rejects_bad() {
        assert_eq!(
            parse_mac("11:22:33:44:55:66").unwrap(),
            [0x11, 0x22, 0x33, 0x44, 0x55, 0x66]
        );
        assert_eq!(
            parse_mac("aa-bb-cc-dd-ee-ff").unwrap(),
            [0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff]
        );
        assert!(parse_mac("11:22:33").is_err(), "too few bytes");
        assert!(parse_mac("gg:22:33:44:55:66").is_err(), "non-hex byte");
    }

    #[test]
    fn parse_color_order_covers_all_six_and_rejects_junk() {
        assert_eq!(parse_color_order("rgb").unwrap(), ColorOrder::Rgb);
        assert_eq!(parse_color_order("BGR").unwrap(), ColorOrder::Bgr);
        assert_eq!(parse_color_order(" grb ").unwrap(), ColorOrder::Grb);
        assert_eq!(parse_color_order("gbr").unwrap(), ColorOrder::Gbr);
        assert_eq!(parse_color_order("rbg").unwrap(), ColorOrder::Rbg);
        assert_eq!(parse_color_order("brg").unwrap(), ColorOrder::Brg);
        assert!(parse_color_order("xyz").is_err());
    }

    #[test]
    fn parse_bool_truthy_set() {
        for t in ["1", "true", "YES", "On"] {
            assert!(parse_bool(t), "{t} should be truthy");
        }
        for fv in ["0", "false", "no", ""] {
            assert!(!parse_bool(fv), "{fv} should be falsey");
        }
    }
}
// ── Linux-only transport + first-light scene ─────────────────────────────────
//
// The socket + link-state + GL paint. Compiled only on Linux (needs libc AF_PACKET
// / glow); verified via the aarch64 cross-build, exercised at first-light on the Pi.
// The whole crate's `cargo test` gate on macOS never sees this block, so all
// *testable* correctness lives in the pure section above (packer, driver, config).

#[cfg(target_os = "linux")]
pub use linux_impl::{link_state, LinkState, RawSocketSink};

#[cfg(target_os = "linux")]
mod linux_impl {
    use super::FrameSink;
    use std::io;
    use std::os::unix::io::RawFd;

    /// `ETH_P_ALL` in host order; `libc` doesn't re-export it. Used as the socket
    /// protocol (as `htons`) — for a send-only SOCK_RAW it just has to be non-zero
    /// and consistent with `sll_protocol`.
    const ETH_P_ALL: u16 = 0x0003;

    /// Linux `AF_PACKET`/`SOCK_RAW` sink: blasts each L2 frame straight onto the NIC.
    /// Requires `CAP_NET_RAW` (root or `setcap cap_net_raw+ep`, design doc §4). Thin
    /// — every wire byte is already correct out of `serialize_frame`; this only does
    /// `sendto` per packet. `serialize_frame` emits FULL frames (14-byte eth header
    /// included), so `SOCK_RAW` sends them verbatim.
    pub struct RawSocketSink {
        fd: RawFd,
        ifindex: i32,
        iface: String,
        dest_mac: [u8; 6],
    }

    impl RawSocketSink {
        /// Open + resolve the interface index. `dest_mac` should match the config's
        /// `dest_mac` (it's already in each frame's header; `sll_addr` is belt-and-
        /// braces for `sendto`).
        pub fn open(iface: &str, dest_mac: [u8; 6]) -> io::Result<Self> {
            let cif = std::ffi::CString::new(iface)
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "iface name has NUL"))?;
            // if_nametoindex: 0 == error (unknown interface).
            let ifindex = unsafe { libc::if_nametoindex(cif.as_ptr()) };
            if ifindex == 0 {
                return Err(io::Error::last_os_error());
            }
            let proto = (ETH_P_ALL.to_be()) as libc::c_int;
            let fd = unsafe { libc::socket(libc::AF_PACKET, libc::SOCK_RAW, proto) };
            if fd < 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(Self {
                fd,
                ifindex: ifindex as i32,
                iface: iface.to_string(),
                dest_mac,
            })
        }

        pub fn iface(&self) -> &str {
            &self.iface
        }
    }

    impl FrameSink for RawSocketSink {
        fn send_frame(&mut self, frames: &[Vec<u8>]) -> io::Result<()> {
            for f in frames {
                let mut addr: libc::sockaddr_ll = unsafe { std::mem::zeroed() };
                addr.sll_family = libc::AF_PACKET as u16;
                addr.sll_protocol = ETH_P_ALL.to_be();
                addr.sll_ifindex = self.ifindex;
                addr.sll_halen = 6;
                addr.sll_addr[..6].copy_from_slice(&self.dest_mac);
                let sent = unsafe {
                    libc::sendto(
                        self.fd,
                        f.as_ptr() as *const libc::c_void,
                        f.len(),
                        0,
                        &addr as *const libc::sockaddr_ll as *const libc::sockaddr,
                        std::mem::size_of::<libc::sockaddr_ll>() as libc::socklen_t,
                    )
                };
                if sent < 0 {
                    return Err(io::Error::last_os_error());
                }
            }
            Ok(())
        }
    }

    impl Drop for RawSocketSink {
        fn drop(&mut self) {
            if self.fd >= 0 {
                unsafe {
                    libc::close(self.fd);
                }
            }
        }
    }

    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum LinkState {
        Up,
        Down,
        Unknown,
    }

    /// Carrier + speed from `/sys/class/net/<if>/{carrier,speed}` (FPP-style, design
    /// doc §8). Never wedges: a missing/unreadable file → `Unknown` / `None`. The arm
    /// warns on down/slow but keeps compositing — the link is not load-bearing for the
    /// renderer, only for pixels reaching the card.
    pub fn link_state(iface: &str) -> (LinkState, Option<u32>) {
        let state = match std::fs::read_to_string(format!("/sys/class/net/{iface}/carrier")) {
            Ok(s) => match s.trim() {
                "1" => LinkState::Up,
                "0" => LinkState::Down,
                _ => LinkState::Unknown,
            },
            Err(_) => LinkState::Unknown,
        };
        let speed = std::fs::read_to_string(format!("/sys/class/net/{iface}/speed"))
            .ok()
            .and_then(|s| s.trim().parse::<i64>().ok())
            .filter(|&n| n > 0)
            .map(|n| n as u32);
        (state, speed)
    }

}
