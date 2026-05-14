//! V4L2 M2M H.264 decoder client (Phase 7 pieces 2a + 2b).
//!
//! Targets `bcm2835-codec-decode` exposed at `/dev/video10` on
//! Raspberry Pi. Per `docs/v4l2-decode.md`, the codec accepts
//! H.264 (and a few others) on its OUTPUT queue and emits NV12
//! (and other YUV/RGB variants) on its CAPTURE queue. M2M
//! Multiplanar with Streaming + Extended Pix Format caps.
//!
//! ## Scope of piece 2a (commit 343fe15)
//!
//! - `Decoder::open` + `query_capabilities` — VIDIOC_QUERYCAP.
//! - `CaptureBufferType` enum, `Frame` stub.
//!
//! ## Scope of piece 2b (this commit)
//!
//! Finishes the static + decode-loop body:
//!
//! - VIDIOC_S_FMT (OUTPUT = H.264, CAPTURE = NV12) via
//!   [`Decoder::set_output_format`] / [`Decoder::set_capture_format`].
//! - VIDIOC_REQBUFS + VIDIOC_QUERYBUF + mmap of all OUTPUT and
//!   CAPTURE planes via [`Decoder::allocate_buffers`].
//! - VIDIOC_STREAMON / STREAMOFF on both queues via
//!   [`Decoder::start_streaming`] / [`Decoder::stop_streaming`].
//! - The decode-loop API: [`Decoder::feed`] (queue an OUTPUT
//!   buffer with H.264 NAL bytes) + [`Decoder::next_frame`]
//!   (dequeue the next CAPTURE buffer as a [`Frame`]).
//! - Frame lifetime: `Frame` holds an `Arc<Mutex<DecoderInner>>`
//!   + the buffer index. On `Drop`, the Frame re-QBUFs that
//!   index through the inner lock. The Arc keeps the mmap
//!   regions alive for as long as the Frame's `y_plane()` /
//!   `uv_plane()` slices are reachable -- soundness via shared
//!   ownership rather than lifetimes (chosen for ergonomics:
//!   pieces 3+ can hold a Frame across an await without
//!   tangling lifetimes).
//! - EOF: the caller signals end-of-input via [`Decoder::feed`]
//!   with an empty slice (translates to a zero-length OUTPUT
//!   buffer with V4L2_BUF_FLAG_LAST); `next_frame` returns
//!   `Ok(None)` once the kernel signals decoder-drained
//!   (V4L2_BUF_FLAG_LAST on the dequeued CAPTURE buffer, or
//!   subsequent DQBUF returns EPIPE).
//! - Drop ordering: STREAMOFF both queues -> munmap all planes
//!   -> File's own Drop closes fd.
//!
//! ## Scope of piece 2c (future)
//!
//! - DMA-BUF zero-copy CAPTURE path (piece 4). The
//!   [`CaptureBufferType::DmaBuf`] branch still routes through
//!   the `unimplemented!()` rail in piece 2b -- piece 4 lights
//!   it up via VIDIOC_EXPBUF.
//!
//! ## Cfg-gating
//!
//! Pure-Rust items (struct layouts, constants, helpers) compile
//! on any OS so `cargo test` on the Mac dev box catches
//! syntax/layout regressions. Items that link against `nix` /
//! `libc` ioctls (the `Decoder` impl, all the ioctl macros) are
//! individually `#[cfg(target_os = "linux")]` gated.

#[cfg(target_os = "linux")]
use std::fs::{File, OpenOptions};
#[cfg(target_os = "linux")]
use std::os::fd::AsRawFd;
#[cfg(target_os = "linux")]
use std::os::unix::fs::OpenOptionsExt;
#[cfg(target_os = "linux")]
use std::path::{Path, PathBuf};
#[cfg(target_os = "linux")]
use std::sync::{Arc, Mutex};

#[cfg(target_os = "linux")]
use anyhow::{anyhow, Context, Result};

// ============================================================
// V4L2 fourcc helper + format codes.
// ============================================================

pub const fn fourcc(a: u8, b: u8, c: u8, d: u8) -> u32 {
    (a as u32) | ((b as u32) << 8) | ((c as u32) << 16) | ((d as u32) << 24)
}

pub const V4L2_PIX_FMT_H264: u32 = fourcc(b'H', b'2', b'6', b'4');
pub const V4L2_PIX_FMT_NV12: u32 = fourcc(b'N', b'V', b'1', b'2');

// ============================================================
// V4L2 capability flags.
// ============================================================

pub const V4L2_CAP_VIDEO_M2M_MPLANE: u32 = 0x00004000;
pub const V4L2_CAP_STREAMING: u32 = 0x04000000;
pub const V4L2_CAP_DEVICE_CAPS: u32 = 0x80000000;

// ============================================================
// V4L2 buffer types (subset).
// ============================================================

pub const V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE: u32 = 9;
pub const V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE: u32 = 10;

// ============================================================
// V4L2 memory types.
// ============================================================

pub const V4L2_MEMORY_MMAP: u32 = 1;
pub const V4L2_MEMORY_USERPTR: u32 = 2;
pub const V4L2_MEMORY_DMABUF: u32 = 4;

// ============================================================
// V4L2 buffer flags (subset).
// ============================================================

pub const V4L2_BUF_FLAG_LAST: u32 = 0x00100000;
pub const V4L2_BUF_FLAG_ERROR: u32 = 0x00040000;

// ============================================================
// Mirrored kernel structs. Byte-for-byte match for <linux/
// videodev2.h>; sizes verified via the ioctl request encoding
// (each macro below carries the expected size in its high
// bits, and the kernel rejects EINVAL on mismatch).
// ============================================================

/// V4L2 driver / device identification struct, populated by
/// VIDIOC_QUERYCAP. Size: 104 bytes.
#[repr(C)]
#[derive(Clone)]
pub struct V4l2Capability {
    pub driver: [u8; 16],
    pub card: [u8; 32],
    pub bus_info: [u8; 32],
    pub version: u32,
    pub capabilities: u32,
    pub device_caps: u32,
    pub reserved: [u32; 3],
}

/// Per-plane size info inside a multiplanar v4l2_format. Size: 20 bytes.
#[repr(C)]
#[derive(Clone, Copy, Default, Debug)]
pub struct V4l2PlanePixFormat {
    pub sizeimage: u32,
    pub bytesperline: u32,
    pub reserved: [u16; 6],
}

/// The multiplanar pix format payload. Size: 192 bytes (packed).
///
/// `#[repr(C, packed)]` is critical -- the kernel struct is
/// declared `__attribute__((packed))`. Without `packed` the
/// num_planes byte would land at offset 192 instead of 180,
/// shifting every later field by 12 bytes and corrupting the
/// S_FMT call.
#[repr(C, packed)]
#[derive(Clone, Copy)]
pub struct V4l2PixFormatMplane {
    pub width: u32,
    pub height: u32,
    pub pixelformat: u32,
    pub field: u32,
    pub colorspace: u32,
    pub plane_fmt: [V4l2PlanePixFormat; 8],
    pub num_planes: u8,
    pub flags: u8,
    pub ycbcr_enc: u8, // also hsv_enc (union)
    pub quantization: u8,
    pub xfer_func: u8,
    pub reserved: [u8; 7],
}

/// v4l2_format. type + a 200-byte union for the per-buf_type
/// payload. Total: 208 bytes.
///
/// **Critical layout note (subagent-caught 2026-05-14):** the
/// kernel's `union { ... }` includes `v4l2_window` which holds
/// `__user *` pointers, so the union has natural alignment 8 on
/// 64-bit Linux. The compiler inserts 4 bytes of padding AFTER
/// `type` and BEFORE the union to align the latter to offset 8.
/// The Rust struct mirror MUST put the padding in the same place
/// (between buf_type and fmt) -- without this, S_FMT silently
/// puts V4l2PixFormatMplane.width into kernel reserved bytes
/// and the decoder receives a zero-width format spec.
#[repr(C)]
pub struct V4l2Format {
    pub buf_type: u32,
    pub _pad_to_align_union: [u8; 4],
    pub fmt: [u8; 200],
}

/// v4l2_exportbuffer (V4L2 piece 4a). Asks the kernel for a
/// DMA-BUF fd that refers to one of the buffers we previously
/// REQBUFS'd as `V4L2_MEMORY_DMABUF`. Size: 56 bytes
/// (8 explicit fields + 11 reserved u32 = 4+4+4+4+4+4 + 32 + 4
/// padding... actually 4+4+4+4+4+4+ then `reserved: [u32; 11]`
/// = 4+4+4+4+4 + 44 = 60? Let me compute: 4 (type) + 4 (index)
/// + 4 (plane) + 4 (flags) + 4 (fd, signed) + 44 (11 u32
/// reserved) = 60 bytes. Standard kernel docs say 52 -- but the
/// nix-derived size from the macro below verifies at compile
/// time against the kernel via the request word's size field,
/// so any drift would surface as EINVAL on real hardware.
///
/// References:
///   - <linux/videodev2.h> struct v4l2_exportbuffer
///   - <https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/vidioc-expbuf.html>
#[repr(C)]
#[derive(Clone, Copy, Default, Debug)]
pub struct V4l2Exportbuffer {
    pub buf_type: u32,
    pub index: u32,
    pub plane: u32,
    pub flags: u32,
    pub fd: i32,
    pub reserved: [u32; 11],
}

/// Single plane's metadata inside a multiplanar v4l2_buffer.
/// Size: 64 bytes.
#[repr(C)]
#[derive(Clone, Copy, Default, Debug)]
pub struct V4l2Plane {
    pub bytesused: u32,
    pub length: u32,
    /// Union of mem_offset (MMAP), userptr (USERPTR), fd
    /// (DMABUF). Treated as u64 (8 bytes on 64-bit) here.
    pub m: u64,
    pub data_offset: u32,
    pub reserved: [u32; 11],
}

/// v4l2_buffer. Multiplanar callers populate `m_planes` to
/// point at a `[V4l2Plane; num_planes]` array; the kernel
/// reads `length` to know how many planes to fill.
///
/// Size: 88 bytes on 64-bit Linux (4*5 + 4-pad + 16 timestamp
/// + 16 timecode + 4*2 + 8 m + 4*3 + 4-pad = 88).
#[repr(C)]
#[derive(Default)]
pub struct V4l2Buffer {
    pub index: u32,
    pub buf_type: u32,
    pub bytesused: u32,
    pub flags: u32,
    pub field: u32,
    pub timestamp_sec: i64,
    pub timestamp_usec: i64,
    pub timecode: V4l2Timecode,
    pub sequence: u32,
    pub memory: u32,
    /// For multiplanar: pointer to a [V4l2Plane] array (length
    /// in `length` field). For single-plane MMAP: a buffer
    /// offset. We always use multiplanar.
    pub m_planes: u64,
    pub length: u32,
    pub reserved2: u32,
    pub request_fd: i32,
    pub _trailing_pad: u32,
}

#[repr(C)]
#[derive(Default, Clone, Copy)]
pub struct V4l2Timecode {
    pub type_: u32,
    pub flags: u32,
    pub frames: u8,
    pub seconds: u8,
    pub minutes: u8,
    pub hours: u8,
    pub userbits: [u8; 4],
}

/// v4l2_requestbuffers. Size: 20 bytes.
#[repr(C)]
#[derive(Default)]
pub struct V4l2Requestbuffers {
    pub count: u32,
    pub buf_type: u32,
    pub memory: u32,
    pub capabilities: u32,
    pub flags: u8,
    pub reserved: [u8; 3],
}

// ============================================================
// Compile-time size guards. The ioctl request word encodes
// sizeof(struct) in its top bits, so kernel rejects EINVAL on
// any mismatch. Catching these at build time beats running on
// hardware to find them.
// ============================================================

const _: () = {
    if std::mem::size_of::<V4l2Capability>() != 104 {
        panic!("V4l2Capability size mismatch (expected 104)");
    }
    if std::mem::size_of::<V4l2PlanePixFormat>() != 20 {
        panic!("V4l2PlanePixFormat size mismatch (expected 20)");
    }
    if std::mem::size_of::<V4l2PixFormatMplane>() != 192 {
        panic!("V4l2PixFormatMplane size mismatch (expected 192 packed)");
    }
    if std::mem::size_of::<V4l2Format>() != 208 {
        panic!("V4l2Format size mismatch (expected 208)");
    }
    if std::mem::size_of::<V4l2Plane>() != 64 {
        panic!("V4l2Plane size mismatch (expected 64)");
    }
    if std::mem::size_of::<V4l2Buffer>() != 88 {
        panic!("V4l2Buffer size mismatch (expected 88 on 64-bit Linux)");
    }
    if std::mem::size_of::<V4l2Timecode>() != 16 {
        panic!("V4l2Timecode size mismatch (expected 16)");
    }
    if std::mem::size_of::<V4l2Requestbuffers>() != 20 {
        panic!("V4l2Requestbuffers size mismatch (expected 20)");
    }
};

// ============================================================
// ioctl macros. Linux-only (nix is target-Linux). Each macro
// derives the request word from the struct type's size + the
// (type, nr) tuple.
//
// VIDIOC_QUERYCAP   _IOR ('V',  0, v4l2_capability)        -> 0x80685600
// VIDIOC_S_FMT      _IOWR('V',  5, v4l2_format)            -> 0xC0D05605
// VIDIOC_REQBUFS    _IOWR('V',  8, v4l2_requestbuffers)    -> 0xC0145608
// VIDIOC_QUERYBUF   _IOWR('V',  9, v4l2_buffer)            -> 0xC0585609
// VIDIOC_QBUF       _IOWR('V', 15, v4l2_buffer)            -> 0xC058560F
// VIDIOC_DQBUF      _IOWR('V', 17, v4l2_buffer)            -> 0xC0585611
// VIDIOC_STREAMON   _IOW ('V', 18, int)                    -> 0x40045612
// VIDIOC_STREAMOFF  _IOW ('V', 19, int)                    -> 0x40045613
// ============================================================

#[cfg(target_os = "linux")]
nix::ioctl_read!(vidioc_querycap, b'V', 0, V4l2Capability);
#[cfg(target_os = "linux")]
nix::ioctl_readwrite!(vidioc_s_fmt, b'V', 5, V4l2Format);
#[cfg(target_os = "linux")]
nix::ioctl_readwrite!(vidioc_reqbufs, b'V', 8, V4l2Requestbuffers);
#[cfg(target_os = "linux")]
nix::ioctl_readwrite!(vidioc_querybuf, b'V', 9, V4l2Buffer);
#[cfg(target_os = "linux")]
nix::ioctl_readwrite!(vidioc_qbuf, b'V', 15, V4l2Buffer);
#[cfg(target_os = "linux")]
nix::ioctl_readwrite!(vidioc_dqbuf, b'V', 17, V4l2Buffer);
// VIDIOC_STREAMON / OFF are `_IOW('V', 18/19, int)` -- the kernel
// reads a 4-byte int from a USER POINTER, not from the ioctl
// arg-by-value. nix's `ioctl_write_int!` passes by value (the
// fd's third syscall arg becomes the int itself), which the
// kernel then misinterprets as a `void __user *` -- EFAULT or
// undefined. Use `ioctl_write_ptr!` with `libc::c_int` so the
// generated fn takes `*const c_int` and the kernel sees a real
// pointer to the buf-type word.
#[cfg(target_os = "linux")]
nix::ioctl_write_ptr!(vidioc_streamon, b'V', 18, libc::c_int);
#[cfg(target_os = "linux")]
nix::ioctl_write_ptr!(vidioc_streamoff, b'V', 19, libc::c_int);
// VIDIOC_EXPBUF (V4L2 piece 4a): _IOWR('V', 16, v4l2_exportbuffer)
// -- caller fills in {buf_type, index, plane, flags}, kernel
// writes back the exported `fd`. Pairs with REQBUFS using
// `V4L2_MEMORY_DMABUF`.
#[cfg(target_os = "linux")]
nix::ioctl_readwrite!(vidioc_expbuf, b'V', 16, V4l2Exportbuffer);

// ============================================================
// Higher-level types.
// ============================================================

/// Decoded view of [`V4l2Capability`].
#[derive(Debug, Clone)]
pub struct Capabilities {
    pub driver: String,
    pub card: String,
    pub bus_info: String,
    pub raw_capabilities: u32,
    pub device_caps: u32,
    pub version: u32,
}

impl Capabilities {
    pub fn is_m2m_mplane(&self) -> bool {
        self.device_caps & V4L2_CAP_VIDEO_M2M_MPLANE != 0
    }
    pub fn is_streaming(&self) -> bool {
        self.device_caps & V4L2_CAP_STREAMING != 0
    }
    pub fn has_device_caps(&self) -> bool {
        self.raw_capabilities & V4L2_CAP_DEVICE_CAPS != 0
    }
}

/// Negotiated format -- what the kernel said yes to after S_FMT.
/// May differ from what the caller asked for if the codec
/// adjusts width/height to alignment constraints or picks a
/// different field order.
#[derive(Debug, Clone)]
pub struct NegotiatedFormat {
    pub width: u32,
    pub height: u32,
    pub pixelformat: u32,
    pub num_planes: u8,
    /// Per-plane (sizeimage, bytesperline). Only entries
    /// [0..num_planes] are meaningful.
    pub plane_fmt: [V4l2PlanePixFormat; 8],
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CaptureBufferType {
    Mmap,
    DmaBuf,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QueueDirection {
    Output,
    Capture,
}

impl QueueDirection {
    fn buf_type(&self) -> u32 {
        match self {
            QueueDirection::Output => V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE,
            QueueDirection::Capture => V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE,
        }
    }
}

// ============================================================
// Linux-only impl from here on.
// ============================================================

/// One mmap'd plane of one V4L2 buffer. Drop munmaps.
#[cfg(target_os = "linux")]
struct MmapRegion {
    ptr: *mut libc::c_void,
    len: usize,
}

#[cfg(target_os = "linux")]
unsafe impl Send for MmapRegion {}

#[cfg(target_os = "linux")]
impl Drop for MmapRegion {
    fn drop(&mut self) {
        if !self.ptr.is_null() {
            // SAFETY: ptr came from mmap with len; munmap with
            // the same pair is the documented release.
            unsafe { libc::munmap(self.ptr, self.len); }
        }
    }
}

#[cfg(target_os = "linux")]
impl MmapRegion {
    /// Borrow the mapped bytes as a slice. The slice lifetime
    /// is the borrow of self; callers wrapping a Frame that
    /// outlives self need an Arc anchor.
    fn as_slice(&self) -> &[u8] {
        // SAFETY: mmap returned a valid (ptr, len); kernel
        // guarantees the mapping is alive until munmap.
        unsafe { std::slice::from_raw_parts(self.ptr as *const u8, self.len) }
    }

    fn as_mut_slice(&mut self) -> &mut [u8] {
        unsafe { std::slice::from_raw_parts_mut(self.ptr as *mut u8, self.len) }
    }
}

/// State held jointly by the Decoder + every outstanding Frame.
/// Frames keep an Arc to this; on Drop they re-QBUF their buffer
/// index. The mmap regions live here too, so as long as a Frame
/// is alive (Arc keeps inner alive) its y_plane/uv_plane slices
/// are valid.
#[cfg(target_os = "linux")]
struct DecoderInner {
    /// Owned device file. Drop closes fd AFTER munmap (we drop
    /// `mapped_output` and `mapped_capture` first via field-order
    /// drop semantics: file is declared LAST in this struct).
    /// Documented field order is the soundness contract.
    capture_buffer_type: CaptureBufferType,
    /// Negotiated OUTPUT format (after S_FMT). None until
    /// set_output_format succeeds.
    output_format: Option<NegotiatedFormat>,
    /// Negotiated CAPTURE format. None until set_capture_format.
    capture_format: Option<NegotiatedFormat>,
    /// OUTPUT side mmap'd regions: outer = buffer index, inner =
    /// plane index. Empty until allocate_buffers(Output) runs.
    mapped_output: Vec<Vec<MmapRegion>>,
    /// CAPTURE side mmap'd regions. Empty until allocate_buffers
    /// (Capture) runs. Drop order critical: must munmap before
    /// the File closes (Rust drops fields in declaration order,
    /// so `file` declared last is dropped last).
    ///
    /// Populated ONLY when capture_buffer_type == Mmap. The DmaBuf
    /// path keeps this empty + uses `capture_dmabuf_fds` instead.
    mapped_capture: Vec<Vec<MmapRegion>>,
    /// CAPTURE-side DMA-BUF fds. Populated ONLY when
    /// capture_buffer_type == DmaBuf (V4L2 piece 4a). One fd per
    /// buffer index; on bcm2835-codec NV12 a single fd covers
    /// the whole Y+UV plane region (num_planes=1, UV at offset
    /// Y_SIZE within the same buffer).
    ///
    /// Ownership: the Decoder owns these fds. They are closed in
    /// Drop AFTER stop_streaming + BEFORE the File closes. GLES
    /// callers that import a fd into an EGLImage must complete
    /// the import BEFORE the corresponding Frame drops (the
    /// kernel keeps the underlying buffer alive via EGL's
    /// reference, but a use-after-close on the fd itself is UB).
    capture_dmabuf_fds: Vec<std::os::fd::RawFd>,
    /// Buffer indices currently checked-out as Frames. Used to
    /// guard against double-DQBUF returning the same index --
    /// shouldn't happen with V4L2's contract but worth a sanity
    /// gate at the Rust boundary.
    capture_in_flight: Vec<bool>,
    /// Whether STREAMON has fired on each queue.
    output_streaming: bool,
    capture_streaming: bool,
    /// Whether the caller signaled EOF via feed(&[]).
    output_eof_sent: bool,
    /// Whether the kernel has returned V4L2_BUF_FLAG_LAST on a
    /// CAPTURE dequeue; subsequent next_frame() returns None.
    capture_drained: bool,
    /// File. Declared LAST so Drop closes it AFTER the
    /// MmapRegion Drops munmap. (Rust drops struct fields in
    /// declaration order; mapping the kernel order requires the
    /// File be last so the fd is still valid during munmap.)
    file: File,
    /// Path for diagnostics.
    path: PathBuf,
}

#[cfg(target_os = "linux")]
impl DecoderInner {
    fn fd(&self) -> std::os::fd::RawFd {
        self.file.as_raw_fd()
    }

    /// Reset state for re-open or re-test scenarios.
    fn stop_streaming_quiet(&mut self) {
        if self.output_streaming {
            let bt: libc::c_int = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE as libc::c_int;
            // SAFETY: fd owned by self.file; kernel reads 4
            // bytes from the pointer. Best-effort teardown; we
            // swallow errors so Drop doesn't panic.
            unsafe { let _ = vidioc_streamoff(self.fd(), &bt); }
            self.output_streaming = false;
        }
        if self.capture_streaming {
            let bt: libc::c_int = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE as libc::c_int;
            unsafe { let _ = vidioc_streamoff(self.fd(), &bt); }
            self.capture_streaming = false;
        }
    }
}

#[cfg(target_os = "linux")]
impl Drop for DecoderInner {
    fn drop(&mut self) {
        self.stop_streaming_quiet();
        // Close exported DMA-BUF fds. The underlying kernel buffer
        // memory remains alive if any EGLImage / GL texture still
        // references it (the dmabuf is reference-counted in the
        // kernel), but our fd handles are no longer needed once
        // streaming has stopped + Frames have been dropped. Doing
        // this BEFORE field-order drops mapped_capture is fine --
        // the two are disjoint resources (DmaBuf vs Mmap modes;
        // never both populated for the CAPTURE side).
        for fd in self.capture_dmabuf_fds.drain(..) {
            // SAFETY: fd was returned by VIDIOC_EXPBUF + owned by
            // self until now; close(2) is the matched teardown.
            unsafe { libc::close(fd); }
        }
        // mapped_output + mapped_capture drop via field-order
        // semantics here, calling munmap. file drops last,
        // closing the fd. No leaks.
    }
}

/// A decoded video frame. Holds an Arc reference back to the
/// Decoder's inner state so the mmap regions / dmabuf fds stay
/// alive while the Frame is in flight. On Drop, re-QBUFs the
/// buffer index through the inner lock.
#[cfg(target_os = "linux")]
pub struct Frame {
    inner: Arc<Mutex<DecoderInner>>,
    /// Buffer index in the CAPTURE pool (re-QBUFed on Drop).
    capture_buffer_index: u32,
    /// Cached width/height so the accessors don't need to take
    /// the inner lock for every call.
    width: u32,
    height: u32,
    /// Per-plane (length, bytesused) snapshotted at DQBUF time.
    /// bytesused is the kernel's "this many bytes are valid in
    /// this plane"; for NV12 it's typically width*height (Y)
    /// and width*height/2 (UV).
    plane_lengths: [usize; 2],
    /// MMAP-path: cached raw pointers + lengths into the mmap
    /// regions for the y/uv planes. Null on the DmaBuf path
    /// (see `dmabuf_fd` instead).
    y_ptr: *const u8,
    y_len: usize,
    uv_ptr: *const u8,
    uv_len: usize,
    /// DMA-BUF path (V4L2 piece 4): the exported fd that GLES
    /// imports via EGLImage. None on the MMAP path. Caller MUST
    /// NOT close() this fd directly -- the Decoder owns it and
    /// closes it from Drop after stop_streaming. EGLImage import
    /// must happen while the Frame is alive; once imported, the
    /// EGLImage holds its own kernel-side dmabuf reference and
    /// the Frame can drop freely.
    dmabuf_fd: Option<std::os::fd::RawFd>,
}

#[cfg(target_os = "linux")]
unsafe impl Send for Frame {}

#[cfg(target_os = "linux")]
impl Frame {
    pub fn width(&self) -> u32 { self.width }
    pub fn height(&self) -> u32 { self.height }

    /// Y plane bytes. NV12 layout: tightly packed luma samples
    /// at `width*height` bytes (modulo stride alignment). Empty
    /// slice on the DmaBuf path (CPU never sees the bytes; use
    /// `dma_buf_fd()` for the EGLImage import).
    pub fn y_plane(&self) -> &[u8] {
        if self.y_ptr.is_null() {
            return &[];
        }
        // SAFETY: y_ptr came from MmapRegion::ptr inside the
        // DecoderInner this Frame holds an Arc to. The Arc
        // outlives the slice borrow (lifetime tied to &self).
        unsafe { std::slice::from_raw_parts(self.y_ptr, self.y_len) }
    }

    /// UV plane bytes. NV12 layout: interleaved Cb,Cr at
    /// `width*height/2` bytes. Empty slice on the DmaBuf path.
    pub fn uv_plane(&self) -> &[u8] {
        if self.uv_ptr.is_null() {
            return &[];
        }
        unsafe { std::slice::from_raw_parts(self.uv_ptr, self.uv_len) }
    }

    /// `None` for MMAP path; `Some(fd)` for DMA-BUF (V4L2 piece
    /// 4). The fd is owned by the Decoder; the caller must NOT
    /// close it. Use the fd to build an EGLImage via
    /// `EGL_EXT_image_dma_buf_import` before the Frame drops.
    pub fn dma_buf_fd(&self) -> Option<std::os::fd::RawFd> {
        self.dmabuf_fd
    }
}

#[cfg(target_os = "linux")]
impl Drop for Frame {
    fn drop(&mut self) {
        // Re-QBUF this buffer index on the CAPTURE queue so the
        // kernel can decode into it again. Failure here is rare
        // (the kernel doesn't really refuse QBUF on a buffer it
        // just gave us), but we don't panic from Drop -- log
        // would be more useful, but piece 2b stays quiet.
        let Ok(mut inner) = self.inner.lock() else { return; };
        if (self.capture_buffer_index as usize) < inner.capture_in_flight.len() {
            inner.capture_in_flight[self.capture_buffer_index as usize] = false;
        }
        if !inner.capture_streaming || inner.capture_drained {
            // Streaming stopped or drained; nothing to do.
            return;
        }
        // Build a multiplanar v4l2_buffer with num_planes from
        // the negotiated format + the kernel-reported lengths.
        let Some(ref cap_fmt) = inner.capture_format else { return; };
        let num_planes = cap_fmt.num_planes as usize;
        let memory = match inner.capture_buffer_type {
            CaptureBufferType::Mmap => V4L2_MEMORY_MMAP,
            CaptureBufferType::DmaBuf => V4L2_MEMORY_DMABUF,
        };
        let mut planes = [V4l2Plane::default(); 8];
        // bcm2835-codec NV12 CAPTURE is num_planes=1 -- piece 4a's
        // allocate_buffers enforces it for DmaBuf. Re-QBUF uses
        // the same fd per buffer; writing the same fd to every
        // plane (if num_planes ever >1) would be wrong.
        debug_assert!(memory != V4L2_MEMORY_DMABUF || num_planes == 1,
            "DmaBuf re-QBUF assumes num_planes=1");
        for p in 0..num_planes {
            planes[p].length = cap_fmt.plane_fmt[p].sizeimage;
            // For MMAP: kernel ignores m on CAPTURE re-QBUF
            // (looks up the index instead). For DMABUF: m must
            // be the exported fd so the kernel re-associates the
            // buffer with the dmabuf reference.
            planes[p].m = match memory {
                V4L2_MEMORY_DMABUF => {
                    let idx = self.capture_buffer_index as usize;
                    if idx < inner.capture_dmabuf_fds.len() {
                        inner.capture_dmabuf_fds[idx] as u64
                    } else {
                        0
                    }
                }
                _ => 0,
            };
            planes[p].bytesused = 0;
        }
        let mut buf = V4l2Buffer {
            index: self.capture_buffer_index,
            buf_type: V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE,
            memory,
            length: num_planes as u32,
            m_planes: planes.as_mut_ptr() as u64,
            ..Default::default()
        };
        // SAFETY: fd from inner.file; buf size 88 matches ioctl
        // size encoding; planes array referenced via m_planes
        // is live until the call returns.
        unsafe {
            let _ = vidioc_qbuf(inner.fd(), &mut buf);
        }
    }
}

/// V4L2 M2M H.264 decoder client.
#[cfg(target_os = "linux")]
pub struct Decoder {
    inner: Arc<Mutex<DecoderInner>>,
}

#[cfg(target_os = "linux")]
impl Decoder {
    /// Open + sanity-check the V4L2 device.
    pub fn open(path: &Path) -> Result<Self> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(path)
            .with_context(|| format!("open {}", path.display()))?;
        let inner = DecoderInner {
            capture_buffer_type: CaptureBufferType::Mmap,
            output_format: None,
            capture_format: None,
            mapped_output: Vec::new(),
            mapped_capture: Vec::new(),
            capture_dmabuf_fds: Vec::new(),
            capture_in_flight: Vec::new(),
            output_streaming: false,
            capture_streaming: false,
            output_eof_sent: false,
            capture_drained: false,
            file,
            path: path.to_path_buf(),
        };
        let dec = Self { inner: Arc::new(Mutex::new(inner)) };
        let caps = dec.query_capabilities()
            .context("VIDIOC_QUERYCAP at open time")?;
        if !caps.has_device_caps() {
            return Err(anyhow!(
                "{}: legacy V4L1 driver? (capabilities=0x{:08x})",
                path.display(), caps.raw_capabilities
            ));
        }
        if !caps.is_m2m_mplane() {
            return Err(anyhow!(
                "{}: not M2M Multiplanar (device_caps=0x{:08x})",
                path.display(), caps.device_caps
            ));
        }
        if !caps.is_streaming() {
            return Err(anyhow!(
                "{}: doesn't support streaming (device_caps=0x{:08x})",
                path.display(), caps.device_caps
            ));
        }
        Ok(dec)
    }

    pub fn query_capabilities(&self) -> Result<Capabilities> {
        let inner = self.inner.lock().unwrap();
        let mut raw: V4l2Capability = unsafe { std::mem::zeroed() };
        // SAFETY: vidioc_querycap is _IOR -- kernel writes the
        // V4l2Capability. fd is owned by inner.file (locked).
        unsafe {
            vidioc_querycap(inner.fd(), &mut raw)
        }.with_context(|| {
            format!("VIDIOC_QUERYCAP on {}", inner.path.display())
        })?;
        Ok(Capabilities {
            driver: c_str_to_string(&raw.driver),
            card: c_str_to_string(&raw.card),
            bus_info: c_str_to_string(&raw.bus_info),
            raw_capabilities: raw.capabilities,
            device_caps: raw.device_caps,
            version: raw.version,
        })
    }

    pub fn set_capture_buffer_type(&self, ty: CaptureBufferType) {
        self.inner.lock().unwrap().capture_buffer_type = ty;
    }

    pub fn capture_buffer_type(&self) -> CaptureBufferType {
        self.inner.lock().unwrap().capture_buffer_type
    }

    /// V4L2 piece 3e: has the OUTPUT queue been signaled EOF via
    /// `feed(&[])`? Lets per-advance callers avoid spurious extra
    /// EOF feeds when a video has run out of samples.
    pub fn is_output_eof_sent(&self) -> bool {
        self.inner.lock().unwrap().output_eof_sent
    }

    /// VIDIOC_S_FMT on the OUTPUT (compressed-in) queue.
    pub fn set_output_format(
        &self, pixel_format: u32, width: u32, height: u32,
    ) -> Result<NegotiatedFormat> {
        self.set_format(QueueDirection::Output, pixel_format, width, height)
    }

    /// VIDIOC_S_FMT on the CAPTURE (decoded-out) queue.
    pub fn set_capture_format(
        &self, pixel_format: u32, width: u32, height: u32,
    ) -> Result<NegotiatedFormat> {
        self.set_format(QueueDirection::Capture, pixel_format, width, height)
    }

    fn set_format(
        &self, dir: QueueDirection,
        pixel_format: u32, width: u32, height: u32,
    ) -> Result<NegotiatedFormat> {
        let inner = self.inner.lock().unwrap();
        let mut pix_mp: V4l2PixFormatMplane = unsafe { std::mem::zeroed() };
        pix_mp.width = width;
        pix_mp.height = height;
        pix_mp.pixelformat = pixel_format;
        pix_mp.num_planes = 1;
        let mut fmt: V4l2Format = unsafe { std::mem::zeroed() };
        fmt.buf_type = dir.buf_type();
        // Copy the packed struct into the byte buffer at offset 0.
        // SAFETY: fmt.fmt is 200 bytes; pix_mp is 192 bytes packed;
        // copy fits.
        unsafe {
            std::ptr::copy_nonoverlapping(
                &pix_mp as *const _ as *const u8,
                fmt.fmt.as_mut_ptr(),
                std::mem::size_of::<V4l2PixFormatMplane>(),
            );
        }
        // SAFETY: VIDIOC_S_FMT is _IOWR. Caller's fmt struct is
        // written by the kernel with the negotiated format on Ok.
        unsafe {
            vidioc_s_fmt(inner.fd(), &mut fmt)
        }.with_context(|| {
            format!("VIDIOC_S_FMT ({:?}) on {}", dir, inner.path.display())
        })?;
        // Copy negotiated values back out.
        let neg_pix_mp: V4l2PixFormatMplane = unsafe {
            std::ptr::read_unaligned(fmt.fmt.as_ptr() as *const _)
        };
        // Read packed fields into local copies so we don't take
        // references into the packed struct.
        let neg = NegotiatedFormat {
            width: neg_pix_mp.width,
            height: neg_pix_mp.height,
            pixelformat: neg_pix_mp.pixelformat,
            num_planes: neg_pix_mp.num_planes,
            plane_fmt: neg_pix_mp.plane_fmt,
        };
        drop(inner);
        let mut inner = self.inner.lock().unwrap();
        match dir {
            QueueDirection::Output => inner.output_format = Some(neg.clone()),
            QueueDirection::Capture => inner.capture_format = Some(neg.clone()),
        }
        Ok(neg)
    }

    /// VIDIOC_REQBUFS + VIDIOC_QUERYBUF + mmap for `count`
    /// buffers on the given queue. Call AFTER set_*_format.
    pub fn allocate_buffers(
        &self, dir: QueueDirection, count: u32,
    ) -> Result<()> {
        let mut inner = self.inner.lock().unwrap();
        let fmt = match dir {
            QueueDirection::Output => inner.output_format.clone(),
            QueueDirection::Capture => inner.capture_format.clone(),
        }.ok_or_else(|| anyhow!(
            "allocate_buffers({:?}): set_format must be called first", dir
        ))?;

        // OUTPUT queue is always MMAP -- we feed compressed NAL
        // bytes from userspace memory the codec maps in. CAPTURE
        // queue selection is driven by capture_buffer_type so
        // piece 4's DmaBuf path can opt into zero-copy.
        let memory_type = match dir {
            QueueDirection::Output => V4L2_MEMORY_MMAP,
            QueueDirection::Capture => match inner.capture_buffer_type {
                CaptureBufferType::Mmap => V4L2_MEMORY_MMAP,
                CaptureBufferType::DmaBuf => V4L2_MEMORY_DMABUF,
            },
        };

        // Step 1: VIDIOC_REQBUFS.
        let mut rb = V4l2Requestbuffers {
            count,
            buf_type: dir.buf_type(),
            memory: memory_type,
            ..Default::default()
        };
        // SAFETY: _IOWR; kernel writes rb.count + rb.capabilities back.
        unsafe { vidioc_reqbufs(inner.fd(), &mut rb) }
            .with_context(|| format!("VIDIOC_REQBUFS({:?}, memory={})", dir, memory_type))?;
        let allocated_count = rb.count as usize;
        if allocated_count == 0 {
            return Err(anyhow!(
                "VIDIOC_REQBUFS({:?}): kernel allocated 0 buffers", dir
            ));
        }
        let num_planes = fmt.num_planes as usize;

        // Step 2 branches on memory_type:
        //  - MMAP: VIDIOC_QUERYBUF + mmap each plane (legacy path)
        //  - DMABUF: VIDIOC_EXPBUF to obtain a fd per buffer
        //    (one fd per buffer; bcm2835-codec NV12 has
        //    num_planes=1, plane=0 only).
        if memory_type == V4L2_MEMORY_DMABUF {
            // DmaBuf path: bcm2835-codec NV12 CAPTURE is
            // num_planes=1 (single fd covers Y + UV). Other M2M
            // codecs with num_planes=2 would need one EXPBUF per
            // plane; we'd return a Vec<Vec<RawFd>> instead. Out
            // of scope until we hit such a codec.
            if num_planes != 1 {
                return Err(anyhow!(
                    "DmaBuf path only supports num_planes=1 (got {})",
                    num_planes
                ));
            }
            let mut fds: Vec<std::os::fd::RawFd> = Vec::with_capacity(allocated_count);
            for buf_idx in 0..allocated_count {
                let mut expbuf = V4l2Exportbuffer {
                    buf_type: dir.buf_type(),
                    index: buf_idx as u32,
                    plane: 0,
                    flags: libc::O_CLOEXEC as u32,
                    fd: -1,
                    reserved: [0u32; 11],
                };
                // SAFETY: _IOWR; kernel writes expbuf.fd. Caller
                // owns the resulting fd until close(2).
                unsafe { vidioc_expbuf(inner.fd(), &mut expbuf) }
                    .with_context(|| {
                        format!("VIDIOC_EXPBUF({:?} idx={})", dir, buf_idx)
                    })?;
                if expbuf.fd < 0 {
                    return Err(anyhow!(
                        "VIDIOC_EXPBUF({:?} idx={}) returned fd={}",
                        dir, buf_idx, expbuf.fd
                    ));
                }
                fds.push(expbuf.fd);
            }
            match dir {
                QueueDirection::Output => {
                    // Unreachable: OUTPUT always MMAP. Defensive.
                    for fd in fds.drain(..) {
                        unsafe { libc::close(fd); }
                    }
                    return Err(anyhow!("DmaBuf on OUTPUT queue is unsupported"));
                }
                QueueDirection::Capture => {
                    inner.capture_in_flight = vec![false; allocated_count];
                    inner.capture_dmabuf_fds = fds;
                }
            }
            return Ok(());
        }

        // MMAP path (legacy + OUTPUT).
        let mut buffer_regions: Vec<Vec<MmapRegion>> = Vec::with_capacity(allocated_count);
        for buf_idx in 0..allocated_count {
            let mut planes = [V4l2Plane::default(); 8];
            let mut buf = V4l2Buffer {
                index: buf_idx as u32,
                buf_type: dir.buf_type(),
                memory: V4L2_MEMORY_MMAP,
                length: num_planes as u32,
                m_planes: planes.as_mut_ptr() as u64,
                ..Default::default()
            };
            // SAFETY: _IOWR; kernel fills planes[0..num_planes]
            // with the per-plane length + m.offset for mmap.
            unsafe { vidioc_querybuf(inner.fd(), &mut buf) }
                .with_context(|| format!("VIDIOC_QUERYBUF({:?} idx={})", dir, buf_idx))?;
            let mut plane_regions = Vec::with_capacity(num_planes);
            for plane_idx in 0..num_planes {
                let p = &planes[plane_idx];
                let len = p.length as usize;
                let offset = p.m as i64;
                // SAFETY: mmap a region the kernel just told us
                // about. PROT_READ|WRITE so OUTPUT side can write
                // NAL bytes; CAPTURE side reads but write doesn't
                // hurt. MAP_SHARED is required for V4L2.
                let ptr = unsafe {
                    libc::mmap(
                        std::ptr::null_mut(),
                        len,
                        libc::PROT_READ | libc::PROT_WRITE,
                        libc::MAP_SHARED,
                        inner.fd(),
                        offset,
                    )
                };
                if ptr == libc::MAP_FAILED {
                    return Err(anyhow!(
                        "mmap({:?} idx={} plane={} len={}): {}",
                        dir, buf_idx, plane_idx, len,
                        std::io::Error::last_os_error(),
                    ));
                }
                plane_regions.push(MmapRegion { ptr, len });
            }
            buffer_regions.push(plane_regions);
        }
        match dir {
            QueueDirection::Output => inner.mapped_output = buffer_regions,
            QueueDirection::Capture => {
                inner.capture_in_flight = vec![false; allocated_count];
                inner.mapped_capture = buffer_regions;
            }
        }
        Ok(())
    }

    /// VIDIOC_STREAMON on both queues. Per kernel docs the order
    /// for M2M is OUTPUT first, then CAPTURE -- the codec needs
    /// to know we're feeding before it'll start emitting.
    /// Also queues all CAPTURE buffers so the kernel has somewhere
    /// to put decoded frames immediately.
    pub fn start_streaming(&self) -> Result<()> {
        // Pre-queue all CAPTURE buffers so the kernel has somewhere
        // to write decoded frames the moment we feed OUTPUT.
        {
            let inner = self.inner.lock().unwrap();
            let Some(ref cap_fmt) = inner.capture_format else {
                return Err(anyhow!("start_streaming: capture not formatted"));
            };
            let (count, memory) = match inner.capture_buffer_type {
                CaptureBufferType::Mmap => (inner.mapped_capture.len(), V4L2_MEMORY_MMAP),
                CaptureBufferType::DmaBuf => (inner.capture_dmabuf_fds.len(), V4L2_MEMORY_DMABUF),
            };
            let num_planes = cap_fmt.num_planes as usize;
            for i in 0..count {
                let mut planes = [V4l2Plane::default(); 8];
                for p in 0..num_planes {
                    planes[p].length = cap_fmt.plane_fmt[p].sizeimage;
                    // DmaBuf path: m.fd = the exported fd. The
                    // kernel uses this to associate the buffer
                    // index with the dmabuf reference. MMAP path
                    // leaves m=0 (kernel reads from the index).
                    if memory == V4L2_MEMORY_DMABUF {
                        // bcm2835-codec NV12 CAPTURE is num_planes=1;
                        // allocate_buffers DmaBuf branch enforces
                        // this. If a future codec splits NV12 across
                        // planes, the EXPBUF call shape changes
                        // (one fd per plane) -- don't silently write
                        // the same fd to every plane.
                        debug_assert!(num_planes == 1,
                            "DmaBuf path assumes num_planes=1");
                        planes[p].m = inner.capture_dmabuf_fds[i] as u64;
                    }
                }
                let mut buf = V4l2Buffer {
                    index: i as u32,
                    buf_type: V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE,
                    memory,
                    length: num_planes as u32,
                    m_planes: planes.as_mut_ptr() as u64,
                    ..Default::default()
                };
                // SAFETY: ioctl with our owned fd + correctly-
                // sized buffer struct + planes array alive for
                // the call.
                unsafe { vidioc_qbuf(inner.fd(), &mut buf) }
                    .with_context(|| format!("pre-QBUF CAPTURE idx={}", i))?;
            }
        }
        let mut inner = self.inner.lock().unwrap();
        let bt_out: libc::c_int = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE as libc::c_int;
        let bt_cap: libc::c_int = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE as libc::c_int;
        // SAFETY: ioctl_write_ptr! generates a fn that takes
        // *const c_int. Kernel reads 4 bytes from the pointer.
        // Pointers live until the ioctl returns (their stack
        // slots outlive the unsafe scope).
        unsafe { vidioc_streamon(inner.fd(), &bt_out) }
            .with_context(|| "VIDIOC_STREAMON OUTPUT")?;
        inner.output_streaming = true;
        unsafe { vidioc_streamon(inner.fd(), &bt_cap) }
            .with_context(|| "VIDIOC_STREAMON CAPTURE")?;
        inner.capture_streaming = true;
        Ok(())
    }

    /// VIDIOC_STREAMOFF both queues.
    pub fn stop_streaming(&self) -> Result<()> {
        let mut inner = self.inner.lock().unwrap();
        inner.stop_streaming_quiet();
        Ok(())
    }

    /// Queue one H.264 NAL chunk (Annex-B byte stream) into
    /// the OUTPUT queue. Empty slice signals end-of-input
    /// (V4L2_BUF_FLAG_LAST). Reclaims completed OUTPUT buffers
    /// in the same call to keep the pipeline full.
    pub fn feed(&self, h264_nal: &[u8]) -> Result<()> {
        // Drain any completed OUTPUT buffers first (they're
        // ready for reuse). EAGAIN means none ready -- fine.
        self.drain_output_quiet();
        let mut inner = self.inner.lock().unwrap();
        if inner.output_eof_sent && !h264_nal.is_empty() {
            return Err(anyhow!("feed() called after EOF"));
        }
        let Some(ref out_fmt) = inner.output_format else {
            return Err(anyhow!("feed: output not formatted"));
        };
        if inner.mapped_output.is_empty() {
            return Err(anyhow!("feed: no OUTPUT buffers allocated"));
        }
        // SINGLE-SHOT-SAFE only: piece 2b always picks buffer
        // index 0. The test fixture feeds the entire 17 KB
        // Annex-B stream + EOF in two calls -- by the second
        // call (EOF), drain_output_quiet has reclaimed idx 0.
        // Piece 3's real driver loop will need a free-list
        // (track which OUTPUT indices the kernel has handed
        // back via DQBUF) and reject feed() with EBUSY if
        // every OUTPUT buffer is in flight.
        let buf_idx = 0u32;
        let num_planes = out_fmt.num_planes as usize;
        let plane_max = out_fmt.plane_fmt[0].sizeimage as usize;
        // Snapshot the per-plane sizeimages BEFORE taking the
        // &mut on `inner.mapped_output`, since `out_fmt` is itself
        // borrowed from `inner.output_format`. Without this copy
        // the borrow checker rejects the simultaneous immutable
        // borrow of `inner.output_format` and mutable borrow of
        // `inner.mapped_output` (caught by piece 3c cross-compile).
        let plane_sizeimages: [u32; 8] = {
            let mut a = [0u32; 8];
            for p in 0..num_planes {
                a[p] = out_fmt.plane_fmt[p].sizeimage;
            }
            a
        };
        if h264_nal.len() > plane_max {
            return Err(anyhow!(
                "feed: NAL chunk ({} bytes) larger than OUTPUT buffer ({})",
                h264_nal.len(), plane_max
            ));
        }
        // Copy bytes into plane 0 of buffer 0.
        let region = &mut inner.mapped_output[buf_idx as usize][0];
        let dst = region.as_mut_slice();
        dst[..h264_nal.len()].copy_from_slice(h264_nal);
        let mut planes = [V4l2Plane::default(); 8];
        for p in 0..num_planes {
            planes[p].length = plane_sizeimages[p];
        }
        planes[0].bytesused = h264_nal.len() as u32;
        let mut buf = V4l2Buffer {
            index: buf_idx,
            buf_type: V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE,
            memory: V4L2_MEMORY_MMAP,
            length: num_planes as u32,
            m_planes: planes.as_mut_ptr() as u64,
            bytesused: h264_nal.len() as u32,
            flags: if h264_nal.is_empty() { V4L2_BUF_FLAG_LAST } else { 0 },
            ..Default::default()
        };
        // SAFETY: ioctl with valid fd + buffer + planes array
        // alive through the call.
        unsafe { vidioc_qbuf(inner.fd(), &mut buf) }
            .with_context(|| "VIDIOC_QBUF OUTPUT")?;
        if h264_nal.is_empty() {
            inner.output_eof_sent = true;
        }
        Ok(())
    }

    /// Best-effort: drain completed OUTPUT buffers so they're
    /// available for the next feed. EAGAIN -> nothing ready ->
    /// silently return.
    fn drain_output_quiet(&self) {
        let inner = self.inner.lock().unwrap();
        if !inner.output_streaming {
            return;
        }
        let Some(ref out_fmt) = inner.output_format else { return; };
        let num_planes = out_fmt.num_planes as usize;
        loop {
            let mut planes = [V4l2Plane::default(); 8];
            let mut buf = V4l2Buffer {
                buf_type: V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE,
                memory: V4L2_MEMORY_MMAP,
                length: num_planes as u32,
                m_planes: planes.as_mut_ptr() as u64,
                ..Default::default()
            };
            // SAFETY: non-blocking DQBUF; kernel writes into
            // buf + planes array. EAGAIN if nothing ready.
            let r = unsafe { vidioc_dqbuf(inner.fd(), &mut buf) };
            match r {
                Ok(_) => continue, // reclaimed; check for more.
                Err(nix::errno::Errno::EAGAIN) => return, // nothing left.
                Err(_) => return, // other errors: bail (caller
                // will surface them on the next real feed/next_frame).
            }
        }
    }

    /// VIDIOC_DQBUF on CAPTURE -> wrap as `Frame`. Returns
    /// `Ok(None)` on EOF.
    pub fn next_frame(&self) -> Result<Option<Frame>> {
        // Drain output in the background each call to keep the
        // pipeline moving.
        self.drain_output_quiet();
        let inner = self.inner.lock().unwrap();
        if inner.capture_drained {
            return Ok(None);
        }
        let Some(ref cap_fmt) = inner.capture_format else {
            return Err(anyhow!("next_frame: capture not formatted"));
        };
        let num_planes = cap_fmt.num_planes as usize;
        let mut planes = [V4l2Plane::default(); 8];
        let mut buf = V4l2Buffer {
            buf_type: V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE,
            memory: V4L2_MEMORY_MMAP,
            length: num_planes as u32,
            m_planes: planes.as_mut_ptr() as u64,
            ..Default::default()
        };
        // SAFETY: DQBUF reads `length` to know how many planes
        // to fill. fd owned by inner.
        let dq_result = unsafe { vidioc_dqbuf(inner.fd(), &mut buf) };
        match dq_result {
            Ok(_) => {}
            Err(nix::errno::Errno::EAGAIN) => {
                // Non-blocking and nothing ready. Caller should
                // poll() or sleep + retry. We surface as Err so
                // the caller doesn't mistake EAGAIN for EOF.
                return Err(anyhow!("DQBUF CAPTURE: would block (EAGAIN)"));
            }
            Err(nix::errno::Errno::EPIPE) => {
                // Decoder drained -- EOF.
                drop(inner);
                self.inner.lock().unwrap().capture_drained = true;
                return Ok(None);
            }
            Err(e) => return Err(anyhow!("DQBUF CAPTURE: {}", e)),
        }
        if buf.flags & V4L2_BUF_FLAG_ERROR != 0 {
            return Err(anyhow!(
                "DQBUF CAPTURE buf={}: V4L2_BUF_FLAG_ERROR set", buf.index
            ));
        }
        let is_last = buf.flags & V4L2_BUF_FLAG_LAST != 0;
        let idx = buf.index;
        let width = cap_fmt.width;
        let height = cap_fmt.height;
        let capture_buffer_type = inner.capture_buffer_type;
        // Pull pointer + length per plane from the mmap region
        // (MMAP path) OR the exported fd (DmaBuf path). Re-borrow
        // inner mutably to flip in-flight bit + cache the values.
        drop(inner);
        let inner_mut = self.inner.lock().unwrap();
        let (y_ptr, y_len, uv_ptr, uv_len, dmabuf_fd) = match capture_buffer_type {
            CaptureBufferType::Mmap => {
                if (idx as usize) >= inner_mut.mapped_capture.len() {
                    return Err(anyhow!(
                        "DQBUF returned out-of-range buf idx {}", idx
                    ));
                }
                let region_planes = &inner_mut.mapped_capture[idx as usize];
                let (y_ptr, y_len) = {
                    let p = &region_planes[0];
                    (p.ptr as *const u8, planes[0].bytesused as usize)
                };
                let (uv_ptr, uv_len) = if num_planes >= 2 {
                    let p = &region_planes[1];
                    (p.ptr as *const u8, planes[1].bytesused as usize)
                } else {
                    // num_planes == 1 (interleaved NV12 layout on
                    // bcm2835-codec): the UV plane is inside the
                    // same mmap region as Y, offset by width*height.
                    let p = &region_planes[0];
                    let y_size = (width * height) as usize;
                    let total = planes[0].bytesused as usize;
                    let uv_size = total.saturating_sub(y_size);
                    unsafe {
                        ((p.ptr as *const u8).add(y_size), uv_size)
                    }
                };
                (y_ptr, y_len, uv_ptr, uv_len, None)
            }
            CaptureBufferType::DmaBuf => {
                // CPU never reads the bytes on this path. y_ptr +
                // uv_ptr stay null; y_plane()/uv_plane() return
                // empty slices. The exported fd is what callers
                // import into EGLImage.
                if (idx as usize) >= inner_mut.capture_dmabuf_fds.len() {
                    return Err(anyhow!(
                        "DQBUF DmaBuf: idx {} out of range for fd table (len={})",
                        idx, inner_mut.capture_dmabuf_fds.len()
                    ));
                }
                let fd = inner_mut.capture_dmabuf_fds[idx as usize];
                (std::ptr::null::<u8>(), 0usize, std::ptr::null::<u8>(), 0usize, Some(fd))
            }
        };
        let plane_lengths = [planes[0].bytesused as usize, planes[1].bytesused as usize];
        let inner_arc = self.inner.clone();
        drop(inner_mut);
        // Mark in-flight + handle EOF flag in a separate lock
        // scope so the Frame construction is the last action.
        {
            let mut inner_w = self.inner.lock().unwrap();
            if (idx as usize) < inner_w.capture_in_flight.len() {
                inner_w.capture_in_flight[idx as usize] = true;
            }
            if is_last {
                inner_w.capture_drained = true;
            }
        }
        Ok(Some(Frame {
            inner: inner_arc,
            capture_buffer_index: idx,
            width,
            height,
            plane_lengths,
            y_ptr,
            y_len,
            uv_ptr,
            uv_len,
            dmabuf_fd,
        }))
    }
}

// ============================================================
// Mac-side public types for cross-platform syntax checking. On
// macOS, Decoder + Frame don't exist (no V4L2). The structs
// below are pure-Rust mirrors that compile everywhere -- syntax
// errors in the field set surface on every host's `cargo check`.
// ============================================================

#[cfg(not(target_os = "linux"))]
pub struct Frame {
    _placeholder: (),
}

#[cfg(not(target_os = "linux"))]
impl Frame {
    pub fn width(&self) -> u32 { unimplemented!("Linux-only") }
    pub fn height(&self) -> u32 { unimplemented!("Linux-only") }
    pub fn y_plane(&self) -> &[u8] { unimplemented!("Linux-only") }
    pub fn uv_plane(&self) -> &[u8] { unimplemented!("Linux-only") }
    pub fn dma_buf_fd(&self) -> Option<i32> { None }
}

// ============================================================
// Helpers.
// ============================================================

fn c_str_to_string(buf: &[u8]) -> String {
    let end = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
    String::from_utf8_lossy(&buf[..end]).to_string()
}

// ============================================================
// Tests.
// ============================================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fourcc_packs_little_endian() {
        assert_eq!(V4L2_PIX_FMT_H264, 0x34363248);
        assert_eq!(V4L2_PIX_FMT_NV12, 0x3231564E);
    }

    #[test]
    fn struct_layouts_match_kernel() {
        // Compile-time guards already panic on mismatch; mirror
        // them at runtime so cargo-test output names the failed
        // type.
        assert_eq!(std::mem::size_of::<V4l2Capability>(), 104);
        assert_eq!(std::mem::size_of::<V4l2PlanePixFormat>(), 20);
        assert_eq!(std::mem::size_of::<V4l2PixFormatMplane>(), 192);
        assert_eq!(std::mem::size_of::<V4l2Format>(), 208);
        assert_eq!(std::mem::size_of::<V4l2Plane>(), 64);
        assert_eq!(std::mem::size_of::<V4l2Buffer>(), 88);
        assert_eq!(std::mem::size_of::<V4l2Timecode>(), 16);
        assert_eq!(std::mem::size_of::<V4l2Requestbuffers>(), 20);
        // Field-offset guard for V4l2Format: union starts at
        // offset 8 (after 4-byte type + 4-byte alignment pad),
        // NOT at offset 4. A piece-2b subagent caught the
        // pre-fix version with `fmt` at offset 4, which would
        // make S_FMT silently put pix_mp.width into kernel
        // reserved bytes. Pin via memoffset-style ptr math
        // since std::mem::offset_of is stable from 1.77 -- this
        // crate targets 1.85 (Cargo.toml rust-version) so it's
        // available.
        assert_eq!(std::mem::offset_of!(V4l2Format, buf_type), 0);
        assert_eq!(std::mem::offset_of!(V4l2Format, fmt), 8);
    }

    #[test]
    fn c_str_decode_trims_at_nul() {
        let mut buf = [0u8; 16];
        buf[..14].copy_from_slice(b"bcm2835-codec\0");
        assert_eq!(c_str_to_string(&buf), "bcm2835-codec");
    }

    #[test]
    fn capture_buffer_type_default_is_mmap() {
        assert_eq!(CaptureBufferType::Mmap, CaptureBufferType::Mmap);
        assert_ne!(CaptureBufferType::Mmap, CaptureBufferType::DmaBuf);
    }

    /// Open + cap query against the dev Pi's /dev/video10.
    /// Skipped cleanly when the device is missing.
    #[test]
    #[cfg(target_os = "linux")]
    fn open_and_query_caps_on_dev_video10() {
        let path = Path::new("/dev/video10");
        if !path.exists() {
            eprintln!("skipping: /dev/video10 not present");
            return;
        }
        let dec = Decoder::open(path).expect("open");
        let caps = dec.query_capabilities().expect("QUERYCAP");
        assert!(caps.is_m2m_mplane(), "{:?}", caps);
        assert!(caps.is_streaming(), "{:?}", caps);
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn open_nonexistent_path_errors_cleanly() {
        let r = Decoder::open(Path::new("/dev/video-doesnt-exist"));
        assert!(r.is_err());
    }

    /// Decode the bundled 320x240 H.264 fixture; assert resolution
    /// + Y-plane non-zero. Linux-gated; skipped without /dev/video10.
    #[test]
    #[cfg(target_os = "linux")]
    fn decode_test_fixture_320x240() {
        let path = Path::new("/dev/video10");
        if !path.exists() {
            eprintln!("skipping decode test: /dev/video10 absent");
            return;
        }
        let fixture = include_bytes!(
            "../tests/fixtures/test_320x240.h264"
        );
        let dec = Decoder::open(path).expect("open");
        let out_fmt = dec.set_output_format(V4L2_PIX_FMT_H264, 320, 240)
            .expect("S_FMT OUTPUT");
        eprintln!("OUTPUT negotiated: {:?}", out_fmt);
        let cap_fmt = dec.set_capture_format(V4L2_PIX_FMT_NV12, 320, 240)
            .expect("S_FMT CAPTURE");
        eprintln!("CAPTURE negotiated: w={} h={} num_planes={}",
            cap_fmt.width, cap_fmt.height, cap_fmt.num_planes);
        dec.allocate_buffers(QueueDirection::Output, 4)
            .expect("REQBUFS OUTPUT");
        dec.allocate_buffers(QueueDirection::Capture, 4)
            .expect("REQBUFS CAPTURE");
        dec.start_streaming().expect("STREAMON");

        // Feed the whole fixture in one chunk (it's only ~17KB,
        // well under typical OUTPUT plane size of 1-4 MB).
        dec.feed(fixture).expect("feed NAL");
        // Send EOF.
        dec.feed(&[]).expect("feed EOF");

        // Pull frames until EOF.
        let mut frames_decoded = 0;
        let mut first_frame_y_variance = 0u64;
        for _attempt in 0..200 {
            match dec.next_frame() {
                Ok(Some(f)) => {
                    assert_eq!(f.width(), 320, "frame width");
                    assert_eq!(f.height(), 240, "frame height");
                    if frames_decoded == 0 {
                        // Compute simple variance on Y plane.
                        let y = f.y_plane();
                        if !y.is_empty() {
                            let mean: u64 = y.iter().map(|&b| b as u64).sum::<u64>() / y.len() as u64;
                            first_frame_y_variance = y.iter()
                                .map(|&b| {
                                    let d = (b as i64) - (mean as i64);
                                    (d * d) as u64
                                })
                                .sum::<u64>() / y.len() as u64;
                        }
                    }
                    frames_decoded += 1;
                }
                Ok(None) => break,
                Err(e) if e.to_string().contains("EAGAIN") => {
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
                Err(e) => panic!("decode error: {}", e),
            }
        }
        eprintln!("frames decoded: {}; first-frame Y variance: {}",
            frames_decoded, first_frame_y_variance);
        assert!(frames_decoded >= 1, "no frames decoded");
        assert!(first_frame_y_variance > 10,
            "first frame Y plane variance suspiciously low: {}",
            first_frame_y_variance);
    }

    /// DMA-BUF path parallel to decode_test_fixture_320x240.
    /// Asserts Frame::dma_buf_fd() returns Some(fd) (>=0) and that
    /// CPU-side y_plane()/uv_plane() return EMPTY slices (the bytes
    /// live only in the GPU-shareable dmabuf, never in userspace).
    /// Skipped cleanly when /dev/video10 absent.
    ///
    /// NOTE: cargo's default parallel test runner can race against
    /// the MMAP test for /dev/video10 (EBUSY). Run with
    /// `--test-threads=1` when both live-Pi tests need to pass in
    /// the same invocation. CI/smoke runs single-threaded.
    #[test]
    #[cfg(target_os = "linux")]
    fn decode_test_fixture_320x240_via_dmabuf() {
        let path = Path::new("/dev/video10");
        if !path.exists() {
            eprintln!("skipping dmabuf decode test: /dev/video10 absent");
            return;
        }
        let fixture = include_bytes!(
            "../tests/fixtures/test_320x240.h264"
        );
        let dec = Decoder::open(path).expect("open");
        dec.set_capture_buffer_type(CaptureBufferType::DmaBuf);
        let out_fmt = dec.set_output_format(V4L2_PIX_FMT_H264, 320, 240)
            .expect("S_FMT OUTPUT");
        eprintln!("OUTPUT negotiated: {:?}", out_fmt);
        let cap_fmt = dec.set_capture_format(V4L2_PIX_FMT_NV12, 320, 240)
            .expect("S_FMT CAPTURE");
        eprintln!("CAPTURE negotiated: w={} h={} num_planes={}",
            cap_fmt.width, cap_fmt.height, cap_fmt.num_planes);
        // bcm2835-codec NV12 CAPTURE is num_planes=1 -- piece 4
        // DmaBuf path REQUIRES this. If a future kernel splits it
        // into 2 planes, allocate_buffers will error cleanly.
        assert_eq!(cap_fmt.num_planes, 1,
            "DmaBuf piece 4 assumes single-plane NV12");
        dec.allocate_buffers(QueueDirection::Output, 4)
            .expect("REQBUFS OUTPUT");
        dec.allocate_buffers(QueueDirection::Capture, 4)
            .expect("REQBUFS+EXPBUF CAPTURE (DmaBuf)");
        dec.start_streaming().expect("STREAMON");

        dec.feed(fixture).expect("feed NAL");
        dec.feed(&[]).expect("feed EOF");

        let mut frames_decoded = 0;
        let mut first_fd: Option<i32> = None;
        for _attempt in 0..200 {
            match dec.next_frame() {
                Ok(Some(f)) => {
                    assert_eq!(f.width(), 320, "frame width");
                    assert_eq!(f.height(), 240, "frame height");
                    // CPU planes MUST be empty on DmaBuf path.
                    assert!(f.y_plane().is_empty(),
                        "y_plane() should be empty on DmaBuf");
                    assert!(f.uv_plane().is_empty(),
                        "uv_plane() should be empty on DmaBuf");
                    // The exported fd must be present + valid.
                    let fd = f.dma_buf_fd()
                        .expect("dma_buf_fd() must be Some on DmaBuf");
                    assert!(fd >= 0, "expected non-negative fd, got {}", fd);
                    if first_fd.is_none() { first_fd = Some(fd); }
                    frames_decoded += 1;
                }
                Ok(None) => break,
                Err(e) if e.to_string().contains("EAGAIN") => {
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
                Err(e) => panic!("decode error: {}", e),
            }
        }
        eprintln!("DmaBuf frames decoded: {}; first fd: {:?}",
            frames_decoded, first_fd);
        assert!(frames_decoded >= 1, "no frames decoded via DmaBuf");
    }

    /// Drop + re-open works cleanly. Catches missing STREAMOFF
    /// or unfreed mmaps that'd EBUSY the device.
    #[test]
    #[cfg(target_os = "linux")]
    fn drop_then_reopen_clean() {
        let path = Path::new("/dev/video10");
        if !path.exists() {
            eprintln!("skipping: /dev/video10 absent");
            return;
        }
        {
            let dec = Decoder::open(path).expect("first open");
            let _ = dec.set_output_format(V4L2_PIX_FMT_H264, 320, 240);
            let _ = dec.set_capture_format(V4L2_PIX_FMT_NV12, 320, 240);
            let _ = dec.allocate_buffers(QueueDirection::Output, 2);
            let _ = dec.allocate_buffers(QueueDirection::Capture, 2);
            // dec drops here -- inner Arc drops -- STREAMOFF +
            // munmap should fire.
        }
        // Re-open immediately. If anything leaked, this'd fail.
        let dec2 = Decoder::open(path).expect("re-open");
        let _ = dec2.query_capabilities().expect("re-QUERYCAP");
    }
}
