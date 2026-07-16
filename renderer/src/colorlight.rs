//! Colorlight 5A-75B TRANSPORT layer.
//!
//! `colorlight_logic.rs` is the pure L2 frame encoder (byte-exact, tested).
//! This module is the transport surface that consumes those frames — the
//! `FrameSink` trait + concrete implementations. All implementations
//! satisfy the same trait so the encoder → sink pipeline is testable
//! on macOS (`VecSink`) and shippable on Linux hardware (`PacketSink`).
//!
//! ## What's here
//!
//! - `FrameSink` — object-safe trait; one `send_frame` per L2 packet, with
//!   a default `send_frames` batch that `PacketSink` overrides with
//!   `sendmmsg(2)` for reduced per-frame syscall overhead.
//! - `VecSink` — records every frame into an in-memory `Vec<Vec<u8>>`, for
//!   loopback tests + property assertions.  Cross-platform.
//! - `StubPacketSink` — structural stand-in that always returns
//!   `TransportUnavailable`.  Cross-platform.  Kept alongside the real
//!   `PacketSink` for host-side dispatch code that wants to name the
//!   "real transport" type without opening a socket.
//! - `PacketSink` (`#[cfg(target_os = "linux")]`) — the real AF_PACKET /
//!   SOCK_RAW transport for the Colorlight receiver card (PR #B1).
//!   Owns a raw socket bound to the operator-selected NIC via
//!   `sockaddr_ll`; sends L2 frames via `sendto` (per-frame path) and
//!   `sendmmsg(2)` (batch path).  RAII Drop closes the socket.
//! - `encode_to_sink` — the encoder → sink glue helper. Takes an
//!   `&mut dyn FrameSink` so callers can pick any sink behind the same
//!   call.

use crate::colorlight_logic::{serialize_frame, ColorlightConfig, SerializeError};

/// Errors surfaced by `FrameSink` implementations.
///
/// Kept intentionally small so the trait stays object-safe + new concrete
/// sinks pick the closest variant. `TransportUnavailable` carries a
/// `String` (not `&'static str`) so a real AF_PACKET sink can attach an
/// errno / interface name / MAC without a caller-visible API bump.
#[derive(Debug)]
pub enum SinkError {
    /// The frame the encoder handed us didn't fit whatever transport
    /// contract the sink enforces (MTU, magic bytes, header offset). The
    /// stub + Vec sinks NEVER emit this; only a wire-level sink would.
    Malformed(&'static str),
    /// Transport backing is not present in this build / at this runtime
    /// state. `StubPacketSink` always emits this; a real socket sink
    /// would emit it on `EACCES` / `ENETDOWN` / etc.
    TransportUnavailable(String),
    /// A `send_frames` batch sent SOME frames but not all — kernel
    /// short-batched (queue pressure / driver anomaly).  Carries
    /// counts so the caller can structurally decide whether to
    /// retry the tail (frames `sent..total`) or accept the
    /// mid-frame row-latch glitch on the sign.  Load-bearing for
    /// PacketSink's `sendmmsg` batch path — mid-frame partial
    /// sends leave the Colorlight card half-updated.
    PartialSend { sent: usize, total: usize },
}

impl std::fmt::Display for SinkError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Malformed(m) => write!(f, "colorlight sink: malformed frame ({m})"),
            Self::TransportUnavailable(m) => write!(f, "colorlight sink: transport unavailable ({m})"),
            Self::PartialSend { sent, total } => write!(
                f,
                "colorlight sink: partial batch send ({sent}/{total} frames delivered; kernel queue pressure or driver anomaly)"
            ),
        }
    }
}

impl std::error::Error for SinkError {}

/// A sink that consumes ready-to-wire L2 frames (14-byte Ethernet header
/// already prepended by `colorlight_logic::serialize_frame`).
///
/// Object-safe by construction: no generic methods, no `Self` returns, no
/// `where Self: Sized` bounds. Callers can hold `Box<dyn FrameSink>` /
/// `&mut dyn FrameSink` at every layer, so swapping VecSink for the real
/// AF_PACKET sink is a one-line change at the pipeline seam.
///
/// Frames arrive in the exact order + shape `serialize_frame` produced:
/// brightness (0x0A) → row packets (0x55) ascending → latch (0x01). A
/// sink MUST forward `send_frame` calls IN ORDER — reordering breaks the
/// card's row-latch semantics.
pub trait FrameSink {
    /// Consume one L2 frame. `frame` is the full Ethernet frame (dst MAC
    /// + src MAC + EtherType + payload); the sink MAY inspect the header
    /// but MUST NOT mutate the bytes.
    fn send_frame(&mut self, frame: &[u8]) -> Result<(), SinkError>;

    /// Consume a batch of frames. Default impl loops over `send_frame`;
    /// a real AF_PACKET sink SHOULD override with `sendmmsg(2)` to
    /// reduce per-frame syscall overhead (128 rows + brightness + latch
    /// = 130 syscalls/frame otherwise, at 30 Hz = ~4k syscalls/sec).
    ///
    /// Stops on first error, returns it. Frames after the failure are
    /// NOT sent — the card is left mid-frame; upstream must decide
    /// whether to retry the whole frame or drop.
    fn send_frames(&mut self, frames: &[Vec<u8>]) -> Result<(), SinkError> {
        for f in frames {
            self.send_frame(f)?;
        }
        Ok(())
    }
}

/// In-memory recording sink for loopback tests + property assertions.
///
/// Every `send_frame` appends to `frames`. Callers reach into `frames`
/// to assert on the recorded stream (counts, opcode ordering, header
/// bytes, per-row payload). Zero overhead beyond `Vec::push`; no
/// filesystem, no socket, works on macOS.
#[derive(Debug, Default)]
pub struct VecSink {
    frames: Vec<Vec<u8>>,
}

impl VecSink {
    pub fn new() -> Self {
        Self::default()
    }

    /// Number of frames the sink has recorded since construction.
    #[inline]
    pub fn frame_count(&self) -> usize {
        self.frames.len()
    }

    /// Read-only view of all recorded frames.
    #[inline]
    pub fn frames(&self) -> &[Vec<u8>] {
        &self.frames
    }

    /// Consume the sink and take ownership of the recorded frames.
    pub fn into_frames(self) -> Vec<Vec<u8>> {
        self.frames
    }
}

impl FrameSink for VecSink {
    fn send_frame(&mut self, frame: &[u8]) -> Result<(), SinkError> {
        self.frames.push(frame.to_vec());
        Ok(())
    }
}

/// Structural stand-in for the eventual real AF_PACKET-backed sink.
/// Always errors on send with `TransportUnavailable` so a pipeline
/// wired to a `StubPacketSink` fails cleanly instead of silently
/// dropping frames.
///
/// Its purpose: let call sites name a concrete "the real sink goes
/// here" type BEFORE the socket lands, so the Phase-1 hardware day is
/// a one-file swap (delete this stub, add `PacketSink` with the same
/// public shape). Fields are placeholders for what a real sink needs
/// so Phase-1 can populate them.
#[derive(Debug)]
pub struct StubPacketSink {
    /// Interface name (e.g. `eth0`) — carried but not used at Phase 0.
    pub ifname: String,
    /// Destination MAC — normally the fixed Colorlight `11:22:33:44:55:66`.
    pub dest_mac: [u8; 6],
}

impl StubPacketSink {
    /// Fixed-Colorlight-defaults constructor. Real transport ignores
    /// these until it opens the socket in Phase 1.
    pub fn new(ifname: impl Into<String>) -> Self {
        Self {
            ifname: ifname.into(),
            dest_mac: crate::colorlight_logic::DEFAULT_DEST_MAC,
        }
    }
}

impl FrameSink for StubPacketSink {
    fn send_frame(&mut self, _frame: &[u8]) -> Result<(), SinkError> {
        Err(SinkError::TransportUnavailable(format!(
            "StubPacketSink({}, dest={:02x?}) — Phase-1 hardware day pending",
            self.ifname, self.dest_mac
        )))
    }
}

// ── Encoder → sink glue ─────────────────────────────────────────────────

/// Combined encoder + sink error — the two failure modes at the pipeline
/// seam. Carries the underlying typed error so operator diagnostics have
/// enough to act on.
#[derive(Debug)]
pub enum EncodeToSinkError {
    Encode(SerializeError),
    Sink(SinkError),
}

impl std::fmt::Display for EncodeToSinkError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Encode(e) => write!(f, "colorlight encode failed: {e:?}"),
            Self::Sink(e) => write!(f, "colorlight sink failed: {e}"),
        }
    }
}

impl std::error::Error for EncodeToSinkError {}

/// Encode one canvas frame + push all resulting L2 frames to `sink` in
/// order. The one-line glue between `serialize_frame` and any `FrameSink`.
///
/// On encoder failure the sink is NOT called (nothing to send). On sink
/// failure the encoder result is dropped (the caller keeps ownership of
/// `fb` for a retry). Neither path mutates `fb` or `cfg`.
pub fn encode_to_sink(
    fb: &[u8],
    cfg: &ColorlightConfig,
    sink: &mut dyn FrameSink,
) -> Result<usize, EncodeToSinkError> {
    let frames = serialize_frame(fb, cfg).map_err(EncodeToSinkError::Encode)?;
    let count = frames.len();
    sink.send_frames(&frames).map_err(EncodeToSinkError::Sink)?;
    Ok(count)
}

// ── PacketSink — real AF_PACKET / SOCK_RAW transport (Linux-only) ────────
//
// The Phase-1 hardware sink.  Opens a raw L2 socket, binds it to the
// operator-selected NIC by ifindex, and blasts each frame the encoder
// produces onto the wire.  Frames arrive from `serialize_frame` with
// the full 14-byte Ethernet header prepended (design-doc §2), so we
// don't add / mutate any header bytes — kernel just DMAs to the NIC.
//
// Discipline notes:
// - **RAII socket ownership.** `PacketSink::open` returns a `PacketSink`;
//   `Drop` calls `libc::close(sockfd)`.  Any `?` in caller code releases
//   the socket cleanly.
// - **sendmmsg batch path.**  A single `serialize_frame` produces 1
//   brightness + N rows + 1 latch = up to hundreds of frames per
//   composited image.  Per-frame `sendto` = per-frame syscall = 4-figure
//   syscalls/sec at cadence.  `sendmmsg(2)` bundles them into one
//   syscall; `send_frames` overrides the trait default.
// - **No `bind(2)`.**  For SOCK_RAW on AF_PACKET, we don't need to bind
//   the socket — the destination sockaddr_ll on each `sendto` carries
//   the ifindex.  Fewer failure paths, matches FPP's approach.
// - **1000 Mbps link requirement.**  Design-doc §2 flags this as a
//   hard prereq (FPP refuses <1000).  This module does NOT check;
//   operator is trusted per Phase-0 discipline.  Warn/refuse gate is
//   a follow-up (would need `/sys/class/net/<if>/speed` read).

#[cfg(target_os = "linux")]
mod packet_sink {
    use super::{FrameSink, SinkError};
    use crate::colorlight_logic::DEFAULT_DEST_MAC;
    use std::ffi::CString;
    use std::io;
    use std::marker::PhantomData;
    use std::os::unix::io::RawFd;

    /// Real AF_PACKET / SOCK_RAW L2 transport for the Colorlight card.
    ///
    /// Owns a raw socket bound to the target NIC (by ifindex).  Each
    /// `send_frame` = one `sendto(2)`; each `send_frames` = one
    /// `sendmmsg(2)` (bundled syscall) for a full serialized frame's
    /// worth of packets.
    ///
    /// **Privilege:** the process needs `CAP_NET_RAW` (root or
    /// `setcap cap_net_raw+ep /path/to/openmarquee-render`).
    /// Design-doc §4 flagged this.  Constructor returns
    /// `TransportUnavailable("EPERM ...")` if the socket open is
    /// denied.
    ///
    /// **Thread-safety:** deliberately `!Send + !Sync` (via the
    /// `PhantomData<*const ()>` marker).  The Colorlight pump is
    /// single-threaded blocking per admin's #B1 anti-list ("no
    /// concurrency in #B1 pump").  Any future edit that tries to
    /// spawn the sink onto a worker thread stops compiling at the
    /// marker, forcing a scope discussion rather than a silent
    /// concurrency regression.
    ///
    /// **No tests in this module.** Constructor + syscall paths need
    /// a real Linux + `CAP_NET_RAW` + a valid ifname; CI runs on
    /// macOS + generic Linux hosts without CAP_NET_RAW.  The pump
    /// layer (Commit 2) exercises the whole path against Phase-1
    /// hardware at first-light; end-to-end integration through
    /// `VecSink` is already covered by PR #88's tests, so the trait
    /// contract is guarded.
    #[derive(Debug)]
    pub struct PacketSink {
        ifname: String,
        ifindex: i32,
        dest_mac: [u8; 6],
        sockfd: RawFd,
        packets_sent: u64,
        batches_sent: u64,
        /// Compile-time marker: PacketSink owns a raw fd; hoisting
        /// it across threads without explicit synchronization is
        /// a footgun.  See struct docstring.
        _not_send_sync: PhantomData<*const ()>,
    }

    impl PacketSink {
        /// Open a raw L2 socket bound to `ifname` for destination MAC
        /// `dest_mac`.  `ETH_P_ALL` protocol (no filter — we're a
        /// sender only; the kernel doesn't filter outbound).
        ///
        /// Fails cleanly with `TransportUnavailable` if:
        /// - The ifname doesn't exist (`if_nametoindex` returns 0).
        /// - The socket open is denied (EPERM = no `CAP_NET_RAW`).
        /// - Any other syscall error.  Errno is captured in the
        ///   message so operators can act on it (EACCES / ENODEV /
        ///   ENETDOWN / etc.).
        pub fn open(ifname: impl Into<String>, dest_mac: [u8; 6]) -> Result<Self, SinkError> {
            let ifname = ifname.into();

            // if_nametoindex — returns 0 on error, else the ifindex.
            let c_ifname = CString::new(ifname.as_bytes()).map_err(|e| {
                SinkError::TransportUnavailable(format!(
                    "colorlight PacketSink: ifname {ifname:?} contains NUL: {e}"
                ))
            })?;
            let ifindex = unsafe { libc::if_nametoindex(c_ifname.as_ptr()) };
            if ifindex == 0 {
                let err = io::Error::last_os_error();
                return Err(SinkError::TransportUnavailable(format!(
                    "colorlight PacketSink: if_nametoindex({ifname:?}) failed: {err}"
                )));
            }
            let ifindex = ifindex as i32;

            // socket(AF_PACKET, SOCK_RAW, 0)
            // Protocol = 0: we're sender-ONLY; leaving the protocol
            // filter at 0 means the kernel does NOT enqueue inbound
            // packets to this socket's receive queue (which we never
            // read).  ETH_P_ALL would cause every inbound Ethernet
            // frame on the host to be copied to this socket's rx
            // buffer — pure book-keeping waste.
            let sockfd = unsafe { libc::socket(libc::AF_PACKET, libc::SOCK_RAW, 0) };
            if sockfd < 0 {
                let err = io::Error::last_os_error();
                return Err(SinkError::TransportUnavailable(format!(
                    "colorlight PacketSink: socket(AF_PACKET, SOCK_RAW) failed: {err} \
                     (CAP_NET_RAW required — see design-doc §4)"
                )));
            }

            // No explicit bind — for SOCK_RAW AF_PACKET sending, the
            // sockaddr_ll passed to sendto carries the ifindex.
            // Matches FPP's approach; fewer failure paths.

            Ok(Self {
                ifname,
                ifindex,
                dest_mac,
                sockfd,
                packets_sent: 0,
                batches_sent: 0,
                _not_send_sync: PhantomData,
            })
        }

        /// Colorlight-defaults constructor — uses `DEFAULT_DEST_MAC`
        /// (`11:22:33:44:55:66`, spec-fixed).
        pub fn open_colorlight_default(ifname: impl Into<String>) -> Result<Self, SinkError> {
            Self::open(ifname, DEFAULT_DEST_MAC)
        }

        /// Total frames successfully sent since construction.
        #[inline]
        pub fn packets_sent(&self) -> u64 {
            self.packets_sent
        }

        /// Number of `send_frames` batches issued via `sendmmsg`.
        /// Included for QA cadence sanity: at 20 Hz with 130 frames
        /// per image, we expect ~20 batches/sec and ~2600 packets/sec.
        #[inline]
        pub fn batches_sent(&self) -> u64 {
            self.batches_sent
        }

        /// Read-only view of the ifname the socket is bound against.
        #[inline]
        pub fn ifname(&self) -> &str {
            &self.ifname
        }

        /// Read-only view of the destination MAC every frame will be
        /// sent to (for operator diagnostics).
        #[inline]
        pub fn dest_mac(&self) -> [u8; 6] {
            self.dest_mac
        }

        /// Build the `sockaddr_ll` for `sendto` at this sink's ifindex
        /// + dest MAC.  Called per-`send_frame`; zero-alloc, stack-
        /// resident.
        fn sockaddr_ll(&self) -> libc::sockaddr_ll {
            let mut sll: libc::sockaddr_ll = unsafe { std::mem::zeroed() };
            sll.sll_family = libc::AF_PACKET as u16;
            // sll_protocol = 0 matches the socket() protocol above
            // (sender-only, no filter).  The kernel uses the
            // EtherType in the frame's L2 header (bytes 12-13,
            // written by `serialize_frame`) for the wire; sll_protocol
            // is only used for INBOUND filtering.
            sll.sll_protocol = 0;
            sll.sll_ifindex = self.ifindex;
            sll.sll_halen = 6;
            // sll_addr is 8 bytes; bytes 6-7 stay zeroed from
            // `mem::zeroed` above — spec-compliant for Ethernet
            // (halen=6 tells the kernel to look at bytes 0-5 only).
            sll.sll_addr[..6].copy_from_slice(&self.dest_mac);
            sll
        }
    }

    impl Drop for PacketSink {
        fn drop(&mut self) {
            // Warn-on-Err so the "why did close fail?" info survives
            // even if the caller returned Ok before drop.  Same
            // discipline as `egl_bringup::tear_down_egl`.
            let rc = unsafe { libc::close(self.sockfd) };
            if rc < 0 {
                let err = io::Error::last_os_error();
                eprintln!(
                    "warn: colorlight PacketSink({}): close({}) failed: {err}",
                    self.ifname, self.sockfd
                );
            }
        }
    }

    impl FrameSink for PacketSink {
        fn send_frame(&mut self, frame: &[u8]) -> Result<(), SinkError> {
            let sll = self.sockaddr_ll();
            let n = unsafe {
                libc::sendto(
                    self.sockfd,
                    frame.as_ptr() as *const libc::c_void,
                    frame.len(),
                    0,
                    &sll as *const libc::sockaddr_ll as *const libc::sockaddr,
                    std::mem::size_of::<libc::sockaddr_ll>() as libc::socklen_t,
                )
            };
            if n < 0 {
                let err = io::Error::last_os_error();
                return Err(SinkError::TransportUnavailable(format!(
                    "colorlight PacketSink({}): sendto failed: {err}",
                    self.ifname
                )));
            }
            let n = n as usize;
            if n != frame.len() {
                // Short-send on a datagram socket = kernel/driver
                // anomaly (queue pressure, MTU mismatch, some
                // driver-specific hiccup) — NOT an encoder / frame
                // shape problem.  Surface as TransportUnavailable
                // with concrete counts so operators can act on it
                // (retry / kick the NIC / check MTU).
                return Err(SinkError::TransportUnavailable(format!(
                    "colorlight PacketSink({}): sendto short-sent ({}/{} bytes; driver / MTU / queue anomaly)",
                    self.ifname,
                    n,
                    frame.len()
                )));
            }
            self.packets_sent += 1;
            Ok(())
        }

        /// Batch send via `sendmmsg(2)` — one syscall for the whole
        /// frame's worth of packets (typically 130 per image at
        /// 128×96).  Trivial batches (`frames.len() <= 1`) skip the
        /// sendmmsg buffer-build overhead and inline the per-frame
        /// `send_frame` call directly (does NOT dispatch through the
        /// trait's default `send_frames` — avoids a redundant loop
        /// layer).
        ///
        /// **Partial-send handling:** if the kernel accepts fewer
        /// messages than requested, returns `SinkError::PartialSend
        /// { sent, total }` so the pump layer can structurally
        /// retry the tail (frames `sent..total`) rather than
        /// leaving the Colorlight card mid-frame with a half-latched
        /// row buffer.  The `packets_sent` + `batches_sent`
        /// counters are updated to reflect what actually landed.
        fn send_frames(&mut self, frames: &[Vec<u8>]) -> Result<(), SinkError> {
            if frames.len() <= 1 {
                // Skip sendmmsg setup for a 1-frame batch — inline
                // the per-frame path directly.
                for f in frames {
                    self.send_frame(f)?;
                }
                return Ok(());
            }

            // Build the mmsghdr / iovec / sockaddr_ll arrays.
            // Everything is stack-lived in this function; sendmmsg
            // returns before we drop.  sockaddr_ll is the same for
            // every message (same dest + ifindex), so one instance
            // is reused via aliased pointer — safe because sendmmsg
            // reads-only.
            let sll = self.sockaddr_ll();
            let mut iovecs: Vec<libc::iovec> = frames
                .iter()
                .map(|f| libc::iovec {
                    iov_base: f.as_ptr() as *mut libc::c_void,
                    iov_len: f.len(),
                })
                .collect();
            let mut msgs: Vec<libc::mmsghdr> = iovecs
                .iter_mut()
                .map(|iov| {
                    let mut hdr: libc::mmsghdr = unsafe { std::mem::zeroed() };
                    hdr.msg_hdr.msg_name =
                        &sll as *const libc::sockaddr_ll as *mut libc::c_void;
                    hdr.msg_hdr.msg_namelen =
                        std::mem::size_of::<libc::sockaddr_ll>() as libc::socklen_t;
                    hdr.msg_hdr.msg_iov = iov as *mut libc::iovec;
                    hdr.msg_hdr.msg_iovlen = 1;
                    hdr
                })
                .collect();

            let n = unsafe {
                libc::sendmmsg(
                    self.sockfd,
                    msgs.as_mut_ptr(),
                    msgs.len() as libc::c_uint,
                    0,
                )
            };
            if n < 0 {
                let err = io::Error::last_os_error();
                return Err(SinkError::TransportUnavailable(format!(
                    "colorlight PacketSink({}): sendmmsg failed: {err}",
                    self.ifname
                )));
            }
            let sent = n as usize;
            if sent != frames.len() {
                // Partial batch send — update counters to reflect
                // what actually landed, return `PartialSend` so the
                // pump layer can structurally decide to retry the
                // tail (`frames[sent..]`) rather than leave the
                // Colorlight card with a half-latched row buffer.
                // The kernel writes per-message `msg_len` in each
                // mmsghdr for the sent ones; not tracked here (all
                // 0-return messages are silently unsent).
                self.packets_sent += sent as u64;
                self.batches_sent += 1;
                return Err(SinkError::PartialSend {
                    sent,
                    total: frames.len(),
                });
            }
            self.packets_sent += frames.len() as u64;
            self.batches_sent += 1;
            Ok(())
        }
    }
}

#[cfg(target_os = "linux")]
pub use packet_sink::PacketSink;

// ── Tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::colorlight_logic::{ColorOrder, ColorlightConfig};

    fn solid(cfg: &ColorlightConfig, r: u8, g: u8, b: u8) -> Vec<u8> {
        let n = cfg.width as usize * cfg.height as usize;
        let mut v = Vec::with_capacity(n * 3);
        for _ in 0..n {
            v.extend_from_slice(&[r, g, b]);
        }
        v
    }

    // ── VecSink ───────────────────────────────────────────────────────

    #[test]
    fn vec_sink_records_frames_in_order() {
        let mut s = VecSink::new();
        s.send_frame(&[1, 2, 3]).unwrap();
        s.send_frame(&[4, 5, 6]).unwrap();
        assert_eq!(s.frame_count(), 2);
        assert_eq!(s.frames()[0], vec![1, 2, 3]);
        assert_eq!(s.frames()[1], vec![4, 5, 6]);
    }

    #[test]
    fn vec_sink_send_frames_default_impl_forwards_every_frame() {
        // The default trait `send_frames` should call `send_frame` in
        // order for each element — assert both the length and the
        // ordering match the batch input.
        let mut s = VecSink::new();
        let batch = vec![vec![10, 20], vec![30, 40], vec![50, 60]];
        s.send_frames(&batch).unwrap();
        assert_eq!(s.frame_count(), 3);
        for (i, f) in batch.iter().enumerate() {
            assert_eq!(&s.frames()[i], f, "frame {i} mismatch");
        }
    }

    #[test]
    fn vec_sink_into_frames_consumes_and_returns_owned_vec() {
        let mut s = VecSink::new();
        s.send_frame(&[7, 8]).unwrap();
        let owned = s.into_frames();
        assert_eq!(owned, vec![vec![7, 8]]);
    }

    // ── StubPacketSink ────────────────────────────────────────────────

    #[test]
    fn stub_packet_sink_always_errors_transport_unavailable() {
        let mut s = StubPacketSink::new("eth0");
        let err = s.send_frame(&[0u8; 64]).unwrap_err();
        match err {
            SinkError::TransportUnavailable(msg) => {
                assert!(msg.contains("StubPacketSink"), "msg: {msg}");
                assert!(msg.contains("eth0"), "msg missing ifname: {msg}");
                assert!(msg.contains("Phase-1"), "msg missing phase note: {msg}");
            }
            other => panic!("expected TransportUnavailable, got {other:?}"),
        }
    }

    #[test]
    fn stub_packet_sink_carries_colorlight_default_dest_mac() {
        let s = StubPacketSink::new("eth0");
        assert_eq!(
            s.dest_mac,
            crate::colorlight_logic::DEFAULT_DEST_MAC,
            "stub sink must carry the Colorlight-spec dest MAC by default"
        );
    }

    /// Test-only sink that COUNTS every `send_frame` call and fails at
    /// a configured index. Lets us pin the "stops on first error"
    /// contract of `FrameSink::send_frames`'s default impl by
    /// observing frames-received-before-the-fail (StubPacketSink's
    /// "every call fails" behaviour hides this).
    struct SpySink {
        recorded: Vec<Vec<u8>>,
        fail_at: Option<usize>,
    }

    impl SpySink {
        fn passthrough() -> Self {
            Self { recorded: Vec::new(), fail_at: None }
        }

        fn fails_at(idx: usize) -> Self {
            Self { recorded: Vec::new(), fail_at: Some(idx) }
        }
    }

    impl FrameSink for SpySink {
        fn send_frame(&mut self, frame: &[u8]) -> Result<(), SinkError> {
            let idx = self.recorded.len();
            if self.fail_at == Some(idx) {
                return Err(SinkError::TransportUnavailable(format!(
                    "spy fail at index {idx}"
                )));
            }
            self.recorded.push(frame.to_vec());
            Ok(())
        }
    }

    #[test]
    fn send_frames_default_impl_stops_on_first_error() {
        // The default `send_frames` MUST stop on the first `?` — real
        // AF_PACKET semantics: a mid-frame failure leaves the card
        // half-latched, so any frame AFTER the failure is worse than
        // useless. Pin by observing SpySink's `recorded` count.
        //
        // Failing at index 2 (frame 3): frames 0 and 1 should be
        // recorded; the send_frames call should error; frame at index
        // 2 was seen (and returned error, so NOT recorded); frames 3+
        // must NOT have been passed to send_frame at all.
        let mut spy = SpySink::fails_at(2);
        let batch = vec![
            vec![10u8; 4],
            vec![20u8; 4],
            vec![30u8; 4],
            vec![40u8; 4],
            vec![50u8; 4],
        ];
        let err = spy.send_frames(&batch).unwrap_err();
        assert!(matches!(err, SinkError::TransportUnavailable(_)));
        assert_eq!(spy.recorded.len(), 2, "must have stopped after 2 successful sends");
        assert_eq!(spy.recorded[0], vec![10u8; 4]);
        assert_eq!(spy.recorded[1], vec![20u8; 4]);
        // The failing index bailed BEFORE recording; frames 3-4 never
        // reached send_frame at all.
    }

    #[test]
    fn stub_packet_sink_send_frames_returns_transport_unavailable() {
        // Complementary to the SpySink coverage above — pins that
        // when the FIRST send fails (StubPacketSink's every-call
        // behavior), send_frames propagates the typed error variant
        // without converting or wrapping.
        let mut s = StubPacketSink::new("wlan0");
        let batch = vec![vec![1u8; 8], vec![2u8; 8], vec![3u8; 8]];
        let err = s.send_frames(&batch).unwrap_err();
        assert!(matches!(err, SinkError::TransportUnavailable(_)));
    }

    // ── SinkError Display ────────────────────────────────────────────

    #[test]
    fn sink_error_display_covers_every_variant() {
        let m = SinkError::Malformed("bad header");
        let s = format!("{m}");
        assert!(s.contains("malformed frame"), "Malformed: {s}");
        assert!(s.contains("bad header"), "Malformed inner: {s}");

        let t = SinkError::TransportUnavailable("EACCES on eth0".to_string());
        let s = format!("{t}");
        assert!(s.contains("transport unavailable"), "TransportUnavailable: {s}");
        assert!(s.contains("EACCES"), "TransportUnavailable inner: {s}");

        // PartialSend variant added in PR #B1 Commit 1 for the
        // PacketSink sendmmsg partial-batch path.  Load-bearing on
        // Colorlight — mid-frame partial sends leave the card half-
        // latched; pump layer keys on the concrete `sent`/`total`
        // fields to decide whether to retry the tail.
        let p = SinkError::PartialSend {
            sent: 65,
            total: 130,
        };
        let s = format!("{p}");
        assert!(s.contains("partial batch send"), "PartialSend: {s}");
        assert!(s.contains("65/130"), "PartialSend inner: {s}");
    }

    // ── encode_to_sink ────────────────────────────────────────────────

    #[test]
    fn encode_to_sink_forwards_all_encoder_frames_to_vec_sink() {
        // Full canvas → encoder → VecSink. Frame count = 1 brightness +
        // card_rows(=128) row packets + 1 latch = 130.
        let cfg = ColorlightConfig::thinksign_default();
        let fb = solid(&cfg, 0, 0, 0);
        let mut sink = VecSink::new();
        let count = encode_to_sink(&fb, &cfg, &mut sink).unwrap();
        assert_eq!(count, 130, "1 brightness + 128 rows + 1 latch");
        assert_eq!(sink.frame_count(), 130, "sink recorded every frame");
    }

    #[test]
    fn encode_to_sink_returns_encoder_error_without_touching_sink() {
        // Encoder rejects: wrong fb size. Sink must never see a call
        // (frame_count stays 0). Proves the pipeline honors the
        // "on encode failure the sink is NOT called" contract.
        let cfg = ColorlightConfig::thinksign_default();
        let too_small = vec![0u8; 10];
        let mut sink = VecSink::new();
        let err = encode_to_sink(&too_small, &cfg, &mut sink).unwrap_err();
        assert!(matches!(err, EncodeToSinkError::Encode(_)));
        assert_eq!(sink.frame_count(), 0, "sink must not receive on encode error");
    }

    #[test]
    fn encode_to_sink_returns_sink_error_from_stub() {
        // Encoder succeeds, StubPacketSink refuses → EncodeToSinkError::Sink.
        // Proves both error paths thread through the typed enum cleanly.
        let cfg = ColorlightConfig::thinksign_default();
        let fb = solid(&cfg, 0, 0, 0);
        let mut sink = StubPacketSink::new("eth0");
        let err = encode_to_sink(&fb, &cfg, &mut sink).unwrap_err();
        assert!(matches!(err, EncodeToSinkError::Sink(SinkError::TransportUnavailable(_))));
    }

    // ── Loopback integration (encoder + VecSink property assertions) ──
    //
    // The dispatch asked for a `renderer/tests/colorlight_loopback.rs`
    // cargo integration test. This crate has no [lib] section (bin-only)
    // + no existing tests/*.rs, so adding one would require restructuring
    // the crate as a library — out of scope for this PR. The tests below
    // exercise the same encoder → sink loopback assertions via the crate-
    // internal cfg(test) mod, which reaches the pub trait + Vec sink
    // exactly as an external caller would. Documented in commit msg.

    #[test]
    fn loopback_solid_black_frame_shape() {
        // The card-visible frame is: 1 brightness (0x0A) + N rows (0x55,
        // ascending) + 1 latch (0x01). Assert opcode ordering + row-
        // number monotonicity, not encoder byte-exactness (colorlight_
        // logic.rs has that). This proves the SINK layer preserved
        // ordering across send_frames' default impl.
        let cfg = ColorlightConfig::thinksign_default();
        let fb = solid(&cfg, 0, 0, 0);
        let mut sink = VecSink::new();
        encode_to_sink(&fb, &cfg, &mut sink).unwrap();
        let frames = sink.frames();
        assert_eq!(frames.len(), 1 + cfg.card_rows() + 1);
        assert_eq!(frames.first().unwrap()[12], 0x0A, "first is brightness");
        assert_eq!(frames.last().unwrap()[12], 0x01, "last is latch");
        for (i, f) in frames[1..=cfg.card_rows()].iter().enumerate() {
            assert_eq!(f[12], 0x55, "packet {i} is row (0x55)");
            let row = ((f[13] as usize) << 8) | f[14] as usize;
            assert_eq!(row, i, "row# ascending: expected {i}, got {row}");
        }
    }

    #[test]
    fn loopback_every_frame_carries_colorlight_dest_mac() {
        // Property: the encoder writes the Colorlight-spec MAC into
        // every frame, and the sink preserves it. If a future refactor
        // strips the header at the sink boundary, this test bites.
        let cfg = ColorlightConfig::thinksign_default();
        let fb = solid(&cfg, 0, 0, 0);
        let mut sink = VecSink::new();
        encode_to_sink(&fb, &cfg, &mut sink).unwrap();
        for (i, f) in sink.frames().iter().enumerate() {
            assert_eq!(
                &f[0..6],
                &crate::colorlight_logic::DEFAULT_DEST_MAC,
                "frame {i} lost dest MAC"
            );
        }
    }

    #[test]
    fn loopback_position_dependent_input_pins_row0_first_pixel() {
        // Solid fills mask per-row byte bugs a position-varying input
        // exposes. Pin: row-0 packet's first payload triple (wire off
        // 21..24) equals fb[0..3] under RGB/no-LUT (the transpose maps
        // canvas col=0/row=0 → card row=0/pos=0 which is offset 21 in
        // packet 1). If the SINK layer ever re-orders or drops the
        // header, this asserts the exact wire byte.
        let cfg = ColorlightConfig::thinksign_default();
        let n = cfg.width as usize * cfg.height as usize;
        let mut fb = Vec::with_capacity(n * 3);
        for k in 0..n {
            fb.extend_from_slice(&[
                (k & 0xff) as u8,
                ((k >> 8) & 0xff) as u8,
                ((k >> 16) & 0xff) as u8,
            ]);
        }
        let mut sink = VecSink::new();
        encode_to_sink(&fb, &cfg, &mut sink).unwrap();
        // Row-0 packet is index 1 (index 0 is brightness).
        let row0 = &sink.frames()[1];
        assert_eq!(row0[12], 0x55, "row0 is a row packet");
        assert_eq!(
            &row0[21..24],
            &fb[0..3],
            "row0 packet's first payload triple is canvas pixel 0"
        );
    }

    #[test]
    fn loopback_non_default_color_order_still_lands_in_sink() {
        // Encoder BGR + solid pure-red → wire byte 21 = B (0), 22 = G (0),
        // 23 = R (255). Sink layer must preserve this — asserts the
        // sink never touches payload bytes even when the encoder
        // config drifts from the default.
        let mut cfg = ColorlightConfig::thinksign_default();
        cfg.color_order = ColorOrder::Bgr;
        let fb = solid(&cfg, 255, 0, 0);
        let mut sink = VecSink::new();
        encode_to_sink(&fb, &cfg, &mut sink).unwrap();
        let row0 = &sink.frames()[1];
        assert_eq!(&row0[21..24], &[0, 0, 255], "BGR + pure R → 0,0,255 wire");
    }

    #[test]
    fn loopback_full_encoder_output_is_bit_exact_between_direct_and_sink() {
        // If I call serialize_frame directly and encode_to_sink into a
        // VecSink, the two Vec<Vec<u8>> outputs must be byte-identical.
        // Any sink-side buffering / mutation / reordering breaks this
        // invariant. Pins the "sink is transparent to encoder bytes"
        // contract.
        let cfg = ColorlightConfig::thinksign_default();
        let fb = solid(&cfg, 42, 84, 168);
        let direct = serialize_frame(&fb, &cfg).unwrap();
        let mut sink = VecSink::new();
        encode_to_sink(&fb, &cfg, &mut sink).unwrap();
        assert_eq!(direct, sink.into_frames());
    }
}
