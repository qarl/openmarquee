"""DRM/KMS renderer — direct kernel-mode-set path for HDMI output.

Phase 6.5 of the plan (qarl 2026-05-01): the legacy `/dev/fb0` path
(see `hdmi.py`) is a kernel compat shim over vc4-kms-v3d. It
forces every frame through a userspace→kernel byte copy AND can't
expose multi-plane composition — both costs that pin Pi Zero 2 W
playback at ~9 fps for full-HD work and make text-over-video at
30 fps physically infeasible (CPU alpha-blend at 1920×1080 = 208
ms in PIL on this CPU; you can't software-composite 2M pixels at
30 Hz on this class of board).

The DRM/KMS path side-steps both:

- A dumb buffer is mmap'd into our address space, so per-frame
  bytes go straight into the display buffer (no syscall copy
  beyond what mmap already established at __enter__).
- Multi-plane composition: text on one plane, background on
  another, GPU composites at scanout. Phase 2a-1 landed the
  atomic-commit infrastructure on a single primary plane.
  Phase 2a-2 added an opt-in overlay plane in ARGB8888.
  Phase 2a-3 (this file's current state) moves the PRIMARY plane
  to the same HVS-scaled, sign-native pattern as the overlay —
  both planes' fbs live at sign dims (e.g. 128×96), the vc4 HVS
  scales them to the letterboxed CRTC region at scanout, and
  per-frame software work for either plane is a sign-native
  swizzle + tiny memcpy. This is the win that fixes the original
  "Welcome transitions are slow" pain: render_frame goes from
  ~100 ms / frame to ~1 ms / frame without changing its API.

This is a Linux-only module; tests on the Mac side mock the
ioctls. The Pi-side live-fire is the canonical correctness check.
"""

from __future__ import annotations

import ctypes
import dataclasses
import fcntl
import logging
import mmap
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops

log = logging.getLogger(__name__)


@dataclasses.dataclass
class _PlaneSlot:
    """One DRM plane + its dumb-buffer fb. Allocated once at __enter__,
    held for the renderer's lifetime; the static-text plane keeps the
    same slot for every slide (its bitmap is overwritten at slide
    entry), animated planes' source bitmaps get rewritten on
    attach_animated_layer for each new slide.

    `attached`: True when the plane is currently bound to the CRTC
    (visible). False = plane disabled (CRTC_ID=0). update_animated_
    layer flips this via the `visible` arg.
    """
    plane_id: int = 0
    props: dict[str, int] = dataclasses.field(default_factory=dict)
    fb_id: int = 0
    dumb_handle: int = 0
    dumb_size: int = 0
    dumb_pitch: int = 0
    mmap: mmap.mmap | None = None
    width: int = 0
    height: int = 0
    attached: bool = False


# --- ioctl encoding ---
# Linux _IOC packs direction + size + group + nr into one 32-bit word.
# DRM uses type 'd' (0x64). Re-derive _IOC instead of pulling
# linux-specific constants — keeps this file portable to a Mac that
# imports the module without exercising the ioctls.

_IOC_WRITE = 1
_IOC_READ = 2


def _IOC(dir_: int, type_: int, nr: int, size: int) -> int:
    return (dir_ << 30) | (type_ << 8) | nr | (size << 16)


def _IOWR(type_: int, nr: int, size: int) -> int:
    return _IOC(_IOC_READ | _IOC_WRITE, type_, nr, size)


def _IOW(type_: int, nr: int, size: int) -> int:
    return _IOC(_IOC_WRITE, type_, nr, size)


_DRM_TYPE = ord("d")


# --- DRM ioctl struct definitions ---
# Layouts pinned to drm_mode.h in linux 6.12 (the dev Pi's kernel).
# Keep field types aligned — uint32/uint64 ordering matters because
# Linux structs aren't naturally aligned across 32/64-bit fields on
# arm64.

class _DrmModeRes(ctypes.Structure):
    _fields_ = [
        ("fb_id_ptr", ctypes.c_uint64),
        ("crtc_id_ptr", ctypes.c_uint64),
        ("connector_id_ptr", ctypes.c_uint64),
        ("encoder_id_ptr", ctypes.c_uint64),
        ("count_fbs", ctypes.c_uint32),
        ("count_crtcs", ctypes.c_uint32),
        ("count_connectors", ctypes.c_uint32),
        ("count_encoders", ctypes.c_uint32),
        ("min_width", ctypes.c_uint32),
        ("max_width", ctypes.c_uint32),
        ("min_height", ctypes.c_uint32),
        ("max_height", ctypes.c_uint32),
    ]


class _DrmModeInfo(ctypes.Structure):
    _fields_ = [
        ("clock", ctypes.c_uint32),
        ("hdisplay", ctypes.c_uint16),
        ("hsync_start", ctypes.c_uint16),
        ("hsync_end", ctypes.c_uint16),
        ("htotal", ctypes.c_uint16),
        ("hskew", ctypes.c_uint16),
        ("vdisplay", ctypes.c_uint16),
        ("vsync_start", ctypes.c_uint16),
        ("vsync_end", ctypes.c_uint16),
        ("vtotal", ctypes.c_uint16),
        ("vscan", ctypes.c_uint16),
        ("vrefresh", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("name", ctypes.c_char * 32),
    ]


class _DrmModeGetConnector(ctypes.Structure):
    _fields_ = [
        ("encoders_ptr", ctypes.c_uint64),
        ("modes_ptr", ctypes.c_uint64),
        ("props_ptr", ctypes.c_uint64),
        ("prop_values_ptr", ctypes.c_uint64),
        ("count_modes", ctypes.c_uint32),
        ("count_props", ctypes.c_uint32),
        ("count_encoders", ctypes.c_uint32),
        ("encoder_id", ctypes.c_uint32),
        ("connector_id", ctypes.c_uint32),
        ("connector_type", ctypes.c_uint32),
        ("connector_type_id", ctypes.c_uint32),
        ("connection", ctypes.c_uint32),
        ("mm_width", ctypes.c_uint32),
        ("mm_height", ctypes.c_uint32),
        ("subpixel", ctypes.c_uint32),
        ("pad", ctypes.c_uint32),
    ]


class _DrmModeGetEncoder(ctypes.Structure):
    _fields_ = [
        ("encoder_id", ctypes.c_uint32),
        ("encoder_type", ctypes.c_uint32),
        ("crtc_id", ctypes.c_uint32),
        ("possible_crtcs", ctypes.c_uint32),
        ("possible_clones", ctypes.c_uint32),
    ]


class _DrmModeCreateDumb(ctypes.Structure):
    _fields_ = [
        ("height", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("bpp", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("handle", ctypes.c_uint32),
        ("pitch", ctypes.c_uint32),
        ("size", ctypes.c_uint64),
    ]


class _DrmModeMapDumb(ctypes.Structure):
    _fields_ = [
        ("handle", ctypes.c_uint32),
        ("pad", ctypes.c_uint32),
        ("offset", ctypes.c_uint64),
    ]


class _DrmModeDestroyDumb(ctypes.Structure):
    _fields_ = [("handle", ctypes.c_uint32)]


class _DrmModeFbCmd2(ctypes.Structure):
    _fields_ = [
        ("fb_id", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("pixel_format", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("handles", ctypes.c_uint32 * 4),
        ("pitches", ctypes.c_uint32 * 4),
        ("offsets", ctypes.c_uint32 * 4),
        ("modifier", ctypes.c_uint64 * 4),
    ]


class _DrmModeCrtc(ctypes.Structure):
    _fields_ = [
        ("set_connectors_ptr", ctypes.c_uint64),
        ("count_connectors", ctypes.c_uint32),
        ("crtc_id", ctypes.c_uint32),
        ("fb_id", ctypes.c_uint32),
        ("x", ctypes.c_uint32),
        ("y", ctypes.c_uint32),
        ("gamma_size", ctypes.c_uint32),
        ("mode_valid", ctypes.c_uint32),
        ("mode", _DrmModeInfo),
    ]


# Atomic / universal-planes structs — used by phase 2a (atomic mode-set,
# multi-plane composition). The atomic API is property-driven: every
# CRTC/connector/plane has named properties addressed by id, and an
# atomic commit is a flat list of (object_id, prop_id, value) tuples
# that the kernel applies (or rejects) as a single transaction.

class _DrmSetClientCap(ctypes.Structure):
    _fields_ = [
        ("capability", ctypes.c_uint64),
        ("value", ctypes.c_uint64),
    ]


class _DrmModeGetPlaneRes(ctypes.Structure):
    _fields_ = [
        ("plane_id_ptr", ctypes.c_uint64),
        ("count_planes", ctypes.c_uint32),
    ]


class _DrmModeGetPlane(ctypes.Structure):
    _fields_ = [
        ("plane_id", ctypes.c_uint32),
        ("crtc_id", ctypes.c_uint32),
        ("fb_id", ctypes.c_uint32),
        ("possible_crtcs", ctypes.c_uint32),
        ("gamma_size", ctypes.c_uint32),
        ("count_format_types", ctypes.c_uint32),
        ("format_type_ptr", ctypes.c_uint64),
    ]


class _DrmModeObjGetProperties(ctypes.Structure):
    _fields_ = [
        ("props_ptr", ctypes.c_uint64),
        ("prop_values_ptr", ctypes.c_uint64),
        ("count_props", ctypes.c_uint32),
        ("obj_id", ctypes.c_uint32),
        ("obj_type", ctypes.c_uint32),
    ]


class _DrmModeGetProperty(ctypes.Structure):
    _fields_ = [
        ("values_ptr", ctypes.c_uint64),
        ("enum_blob_ptr", ctypes.c_uint64),
        ("prop_id", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("name", ctypes.c_char * 32),
        ("count_values", ctypes.c_uint32),
        ("count_enum_blobs", ctypes.c_uint32),
    ]


class _DrmModeAtomic(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("count_objs", ctypes.c_uint32),
        ("objs_ptr", ctypes.c_uint64),
        ("count_props_ptr", ctypes.c_uint64),
        ("props_ptr", ctypes.c_uint64),
        ("prop_values_ptr", ctypes.c_uint64),
        ("reserved", ctypes.c_uint64),
        ("user_data", ctypes.c_uint64),
    ]


class _DrmModeCreateBlob(ctypes.Structure):
    _fields_ = [
        ("data", ctypes.c_uint64),
        ("length", ctypes.c_uint32),
        ("blob_id", ctypes.c_uint32),
    ]


class _DrmModeDestroyBlob(ctypes.Structure):
    _fields_ = [("blob_id", ctypes.c_uint32)]


# --- ioctl numbers ---

DRM_IOCTL_SET_CLIENT_CAP        = _IOW (_DRM_TYPE, 0x0D, ctypes.sizeof(_DrmSetClientCap))
DRM_IOCTL_MODE_GETRESOURCES     = _IOWR(_DRM_TYPE, 0xA0, ctypes.sizeof(_DrmModeRes))
DRM_IOCTL_MODE_GETCRTC          = _IOWR(_DRM_TYPE, 0xA1, ctypes.sizeof(_DrmModeCrtc))
DRM_IOCTL_MODE_SETCRTC          = _IOWR(_DRM_TYPE, 0xA2, ctypes.sizeof(_DrmModeCrtc))
DRM_IOCTL_MODE_GETENCODER       = _IOWR(_DRM_TYPE, 0xA6, ctypes.sizeof(_DrmModeGetEncoder))
DRM_IOCTL_MODE_GETCONNECTOR     = _IOWR(_DRM_TYPE, 0xA7, ctypes.sizeof(_DrmModeGetConnector))
DRM_IOCTL_MODE_GETPROPERTY      = _IOWR(_DRM_TYPE, 0xAA, ctypes.sizeof(_DrmModeGetProperty))
DRM_IOCTL_MODE_RMFB             = _IOWR(_DRM_TYPE, 0xAF, ctypes.sizeof(ctypes.c_uint32))
DRM_IOCTL_MODE_CREATE_DUMB      = _IOWR(_DRM_TYPE, 0xB2, ctypes.sizeof(_DrmModeCreateDumb))
DRM_IOCTL_MODE_MAP_DUMB         = _IOWR(_DRM_TYPE, 0xB3, ctypes.sizeof(_DrmModeMapDumb))
DRM_IOCTL_MODE_DESTROY_DUMB     = _IOWR(_DRM_TYPE, 0xB4, ctypes.sizeof(_DrmModeDestroyDumb))
DRM_IOCTL_MODE_GETPLANERESOURCES= _IOWR(_DRM_TYPE, 0xB5, ctypes.sizeof(_DrmModeGetPlaneRes))
DRM_IOCTL_MODE_GETPLANE         = _IOWR(_DRM_TYPE, 0xB6, ctypes.sizeof(_DrmModeGetPlane))
DRM_IOCTL_MODE_ADDFB2           = _IOWR(_DRM_TYPE, 0xB8, ctypes.sizeof(_DrmModeFbCmd2))
DRM_IOCTL_MODE_OBJ_GETPROPERTIES= _IOWR(_DRM_TYPE, 0xB9, ctypes.sizeof(_DrmModeObjGetProperties))
DRM_IOCTL_MODE_ATOMIC           = _IOWR(_DRM_TYPE, 0xBC, ctypes.sizeof(_DrmModeAtomic))
DRM_IOCTL_MODE_CREATEPROPBLOB   = _IOWR(_DRM_TYPE, 0xBD, ctypes.sizeof(_DrmModeCreateBlob))
DRM_IOCTL_MODE_DESTROYPROPBLOB  = _IOWR(_DRM_TYPE, 0xBE, ctypes.sizeof(_DrmModeDestroyBlob))


# --- DRM constants ---

DRM_MODE_CONNECTED = 1

# fourcc — from drm_fourcc.h. XR24 (XRGB8888) is the safest "most
# vc4 planes accept this" format. RG16 (RGB565) also widely
# supported. We pick XR24 here so plane composition (next phase) has
# room for the alpha channel later.
def _fourcc(a: str, b: str, c: str, d: str) -> int:
    return ord(a) | (ord(b) << 8) | (ord(c) << 16) | (ord(d) << 24)


DRM_FORMAT_XRGB8888 = _fourcc("X", "R", "2", "4")
DRM_FORMAT_ARGB8888 = _fourcc("A", "R", "2", "4")
DRM_FORMAT_RGB565   = _fourcc("R", "G", "1", "6")


# Client capability flags — opt into universal-planes (so non-primary
# planes show up in GETPLANERESOURCES) and atomic (so DRM_IOCTL_MODE_ATOMIC
# is permitted for our DRM master). Both must be set BEFORE any plane
# enumeration or atomic commit is attempted.
DRM_CLIENT_CAP_UNIVERSAL_PLANES = 2
DRM_CLIENT_CAP_ATOMIC = 3

# Plane types — value of the plane object's "type" property. Discovered
# via OBJ_GETPROPERTIES on each plane id.
DRM_PLANE_TYPE_OVERLAY = 0
DRM_PLANE_TYPE_PRIMARY = 1
DRM_PLANE_TYPE_CURSOR = 2

# Object types for OBJ_GETPROPERTIES — distinct sentinel values per
# object class, defined in drm_mode.h.
DRM_MODE_OBJECT_CRTC = 0xCCCCCCCC
DRM_MODE_OBJECT_CONNECTOR = 0xC0C0C0C0
DRM_MODE_OBJECT_PLANE = 0xEEEEEEEE

# Atomic commit flags. ALLOW_MODESET is required for the initial
# mode-set commit; per-frame commits leave it off. NONBLOCK lets
# the commit return immediately instead of waiting for the next
# vblank ack -- used when commit is racing a primary-plane PageFlip
# on the same CRTC (during shader transitions, #214). Without
# NONBLOCK the kernel serializes commits on the CRTC so our overlay
# update waits for the shader's pending PageFlip to land, blowing
# the per-frame budget.
DRM_MODE_ATOMIC_ALLOW_MODESET = 0x0400
DRM_MODE_ATOMIC_NONBLOCK = 0x0200

# pixel blend mode enum values — stable kernel-defined constants from
# include/drm/drm_blend.h, exposed by drm_plane_create_blend_mode_property().
# We pin PREMULTI on every animated plane attach (the vc4 default, but
# the property persists across DRM master sessions so we can't trust
# inheritance). PREMULTI requires premultiplied RGB input, which
# `_write_plane_buffer_subregion` produces. COVERAGE is documented but
# vc4-broken — see "vc4 alpha-handling gotchas" in
# docs/multi-plane-gpu-compositor.md. PIXEL_NONE is unused.
DRM_MODE_BLEND_PIXEL_NONE = 0
DRM_MODE_BLEND_PREMULTI = 1
DRM_MODE_BLEND_COVERAGE = 2


def _ioctl(fd: int, request: int, arg) -> None:
    """Run an ioctl with a ctypes struct, raising OSError on failure.

    Wrapper around fcntl.ioctl that handles the in/out struct dance —
    DRM ioctls usually mutate the struct in place to return data.
    """
    fcntl.ioctl(fd, request, arg)


class DRMRenderer:
    """Render RGB888 frames to an HDMI display via DRM/KMS direct mode-set.

    Mirrors HDMIRenderer's protocol (`width`, `height`, `render_frame(bytes)`)
    so PlaybackLoop can swap one for the other without changes. With
    `enable_overlay=True` an additional ARGB8888 overlay plane is
    allocated at sign-native dims; `render_composite(primary_rgb,
    overlay_rgba)` updates one or both planes and the GPU composites
    overlay-over-primary at scanout. The overlay path lets text with
    alpha animate at video rate without paying the 1080p software
    alpha-blend cost (~208 ms/frame on Pi Zero 2 W) that pinned the
    fb0 path well below 30 fps for any text-over-background work.

    Args:
        width, height: Sign-side dims (what the playback engine emits).
        display_width, display_height: HDMI dims. Defaults to the connector's
            preferred mode (typically 1920×1080 on the dev Pi).
        device_path: DRM card device. Defaults to /dev/dri/card0.
        pixel_format: "rgb565" (2 bytes/pixel, ~30 ms convert at 1080p) or
            "xrgb8888" (4 bytes/pixel, ~80 ms convert at 1080p — needed when
            we add an overlay plane with alpha in the next phase). Default
            is rgb565 — fastest single-plane path on Pi Zero 2 W.
    """

    def __init__(
        self,
        width: int,
        height: int,
        *,
        display_width: int | None = None,
        display_height: int | None = None,
        device_path: Path = Path("/dev/dri/card0"),
        pixel_format: str = "rgb565",
        enable_overlay: bool = False,
        max_animated_planes: int = 0,
        max_pool_buffers: int = 20,
    ):
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive")
        if pixel_format not in ("rgb565", "xrgb8888"):
            raise ValueError(f"unsupported pixel_format {pixel_format!r}")
        if max_animated_planes < 0:
            raise ValueError("max_animated_planes must be >= 0")
        if enable_overlay and max_animated_planes > 0:
            raise ValueError(
                "enable_overlay is the legacy single-overlay API; "
                "use max_animated_planes>0 for the GPU compositor instead"
            )
        self.width = width
        self.height = height
        self.pixel_format = pixel_format
        self._bytes_per_pixel = 2 if pixel_format == "rgb565" else 4
        self._drm_format = (
            DRM_FORMAT_RGB565 if pixel_format == "rgb565" else DRM_FORMAT_XRGB8888
        )
        self.enable_overlay = bool(enable_overlay)
        self.device_path = Path(device_path)
        self._fd: int | None = None
        self._fb_id: int = 0
        self._dumb_handle: int = 0
        self._dumb_size: int = 0
        self._dumb_pitch: int = 0
        self._mmap: mmap.mmap | None = None
        # Per-slide primary buffer pool (#218 part 2). Each entry is a
        # fully-allocated dumb buffer + fb_id + mmap, painted with a
        # specific slide's bg+statics in renderer-native format. At
        # slide attach we just stage FB_ID = pool[slide_id].fb_id in
        # the atomic commit -- ZERO memcpy on the critical path. The
        # painting happens during steady-state in a background thread,
        # so the encode + write into the mmap are off the asyncio main
        # thread entirely. LRU-ordered; soft-capped at 20 slides
        # (~80 MB at 1080p RGB565) to bound memory on Pi Zero 2 W.
        # Once a slide has been visited once and is in the pool, every
        # subsequent attach is a single FB_ID flip -- the screen
        # freeze drops to the kernel's vblank floor (~16 ms).
        from collections import OrderedDict
        # Value: (fb_id, dumb_handle, mmap, content_version). The
        # content_version (caller-supplied opaque token, typically
        # slide.updated_at) lets prepare_primary_buffer detect when
        # a cached buffer's content is stale and needs repaint --
        # without it, an operator editing a slide mid-loop would see
        # the old pixels until LRU eviction.
        self._primary_buffer_pool: OrderedDict[
            object, tuple[int, int, mmap.mmap, object],
        ] = OrderedDict()
        if max_pool_buffers < 0:
            raise ValueError("max_pool_buffers must be >= 0")
        self._max_pool_buffers: int = max_pool_buffers
        self._mode: _DrmModeInfo | None = None
        self._connector_id: int = 0
        self._encoder_id: int = 0
        self._crtc_id: int = 0
        # Bit position of self._crtc_id in the resources' crtcs[] order —
        # used to filter planes by their possible_crtcs bitmask.
        self._crtc_bit: int = 0
        self._original_crtc: _DrmModeCrtc | None = None
        # Atomic / multi-plane state. Empty until _open() runs.
        self._primary_plane_id: int = 0
        self._mode_blob_id: int = 0
        self._crtc_props: dict[str, int] = {}
        self._connector_props: dict[str, int] = {}
        self._primary_plane_props: dict[str, int] = {}
        # Overlay plane state — populated only when enable_overlay=True.
        # Always 4 bytes/pixel ARGB8888 so alpha lands the way the GPU
        # expects it for scanout-time compositing on top of the primary.
        # The overlay fb is held at sign-native dims; vc4's HVS scales
        # it to the letterboxed CRTC region at scanout. That's what
        # turns "alpha text overlaid on a background" from a 200 ms/
        # frame software composite into a sub-millisecond memcpy.
        self._overlay_plane_id: int = 0
        self._overlay_plane_props: dict[str, int] = {}
        self._overlay_fb_id: int = 0
        self._overlay_dumb_handle: int = 0
        self._overlay_dumb_size: int = 0
        self._overlay_dumb_pitch: int = 0
        self._overlay_mmap: mmap.mmap | None = None
        # Letterbox geometry on the CRTC — sign-native overlay fb gets
        # scaled into this rect by the HVS. Set in _open() after the
        # connector's preferred mode is known.
        self._scaled_w: int = 0
        self._scaled_h: int = 0
        self._letterbox_x: int = 0
        self._letterbox_y: int = 0
        # GPU-compositor state (max_animated_planes > 0). N animated
        # overlay planes (zpos=2..N+1), each one motion-animated
        # layer's pre-rasterized RGBA bitmap. Per-frame motion =
        # atomic-commit changing each plane's CRTC_X/Y/W/H / alpha —
        # zero per-pixel CPU.
        #
        # No dedicated static-text plane: vc4 LBM ceiling at 1080p is
        # ~3 simultaneously bound planes (primary + 2 overlays). The
        # bg + all motion=static text layers software-composite into
        # the primary plane ONCE at slide entry via the existing
        # render_frame() path; that frees both overlay slots for
        # animated layers (qarl 2026-05-02 architectural choice
        # combining options 2+3 from the LBM-finding follow-up).
        #
        # Per-plane LBM consumption on vc4 scales with SRC_W (the
        # source rect width the HVS reads per scanline), NOT fb
        # width. So animated planes' fbs stay allocated at max sign
        # dims, but attach_animated_layer takes a glyph-bbox subset
        # — SRC_W/H = bbox dims, CRTC_W/H = display-pixel dest rect.
        # Smaller bbox = less LBM = more simultaneous animated layers.
        self.max_animated_planes = max(0, int(max_animated_planes))
        self._animated_planes: list[_PlaneSlot] = []
        # Pending atomic-property changes staged between commits. Maps
        # (plane_id, prop_id) → value. commit() drains it into one
        # DRM_IOCTL_MODE_ATOMIC and clears.
        self._pending_props: dict[tuple[int, int], int] = {}
        # Override display dims if caller specified — otherwise we'll
        # detect from the connector's preferred mode at __enter__.
        self._explicit_display_w = display_width
        self._explicit_display_h = display_height
        self.display_width = display_width if display_width is not None else width
        self.display_height = display_height if display_height is not None else height

    def __enter__(self) -> DRMRenderer:
        self._open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def _open(self) -> None:
        if self._fd is not None:
            return
        self._fd = os.open(self.device_path, os.O_RDWR | os.O_CLOEXEC)
        try:
            self._set_client_caps()
            self._discover()
            self._compute_letterbox()
            self._discover_planes()
            self._discover_properties()
            self._allocate_framebuffer()
            if self.enable_overlay:
                self._discover_overlay_plane()
                self._allocate_overlay_framebuffer()
            if self.max_animated_planes > 0:
                self._discover_compositor_planes()
            self._atomic_modeset()
        except Exception:
            self.close()
            raise

    def _compute_letterbox(self) -> None:
        """Letterbox the sign rect into the CRTC rect: same arithmetic
        primary-plane software-scaling already uses, but pulled into a
        member so the overlay plane's HVS scaling matches the primary's
        letterboxed content region exactly."""
        scale = min(
            self.display_width / self.width,
            self.display_height / self.height,
        )
        self._scaled_w = max(1, int(round(self.width * scale)))
        self._scaled_h = max(1, int(round(self.height * scale)))
        self._letterbox_x = (self.display_width - self._scaled_w) // 2
        self._letterbox_y = (self.display_height - self._scaled_h) // 2

    # --- discovery ---

    def _discover(self) -> None:
        """Walk DRM resources to find the HDMI connector + a working CRTC.

        Picks the FIRST connected connector (typically HDMI-A-1 on the
        dev Pi). Picks the FIRST mode the kernel marked "preferred" —
        that's the EDID-derived native mode. Picks the first CRTC the
        connector's encoder lists in possible_crtcs.
        """
        assert self._fd is not None
        # GETRESOURCES — first call returns counts, second populates arrays.
        res = _DrmModeRes()
        _ioctl(self._fd, DRM_IOCTL_MODE_GETRESOURCES, res)
        connectors = (ctypes.c_uint32 * res.count_connectors)()
        encoders = (ctypes.c_uint32 * res.count_encoders)()
        crtcs = (ctypes.c_uint32 * res.count_crtcs)()
        res.connector_id_ptr = ctypes.cast(connectors, ctypes.c_void_p).value or 0
        res.encoder_id_ptr = ctypes.cast(encoders, ctypes.c_void_p).value or 0
        res.crtc_id_ptr = ctypes.cast(crtcs, ctypes.c_void_p).value or 0
        _ioctl(self._fd, DRM_IOCTL_MODE_GETRESOURCES, res)

        # Find a connected connector with at least one mode. Initialize
        # `conn_encs` defensively so a connector-loop that never reaches
        # the body's encoder-array allocation doesn't NameError downstream.
        chosen_conn = None
        chosen_mode = None
        conn_encs: tuple = ()
        for cid in connectors:
            conn = _DrmModeGetConnector()
            conn.connector_id = cid
            _ioctl(self._fd, DRM_IOCTL_MODE_GETCONNECTOR, conn)
            if conn.connection != DRM_MODE_CONNECTED or conn.count_modes == 0:
                continue
            modes = (_DrmModeInfo * conn.count_modes)()
            conn_encs = (ctypes.c_uint32 * conn.count_encoders)()
            conn.modes_ptr = ctypes.cast(modes, ctypes.c_void_p).value or 0
            conn.encoders_ptr = ctypes.cast(conn_encs, ctypes.c_void_p).value or 0
            # props_ptr / prop_values_ptr left null — we don't read props here.
            conn.count_props = 0
            _ioctl(self._fd, DRM_IOCTL_MODE_GETCONNECTOR, conn)
            chosen_conn = conn
            # Pick a mode: prefer caller's explicit display dims, then
            # the kernel's "preferred" flag, then first mode.
            DRM_MODE_TYPE_PREFERRED = 1 << 3
            chosen_mode = None
            if self._explicit_display_w and self._explicit_display_h:
                for m in modes:
                    if m.hdisplay == self._explicit_display_w and m.vdisplay == self._explicit_display_h:
                        chosen_mode = m
                        break
            if chosen_mode is None:
                for m in modes:
                    if m.type & DRM_MODE_TYPE_PREFERRED:
                        chosen_mode = m
                        break
            if chosen_mode is None:
                chosen_mode = modes[0]
            self._connector_id = cid
            # Reuse the connector's existing encoder if any; otherwise
            # walk encoders and pick the first that lists a usable CRTC.
            self._encoder_id = conn.encoder_id
            break

        if chosen_conn is None or chosen_mode is None:
            raise RuntimeError("no connected DRM connector with modes")

        # Encoder → tells us which CRTCs are usable.
        if self._encoder_id == 0:
            # No active encoder bound; pick first connector-listed encoder.
            for eid in conn_encs:
                self._encoder_id = eid
                break
        if self._encoder_id == 0:
            raise RuntimeError("connector has no encoder")
        enc = _DrmModeGetEncoder()
        enc.encoder_id = self._encoder_id
        _ioctl(self._fd, DRM_IOCTL_MODE_GETENCODER, enc)
        # possible_crtcs is a bitmask over the resources' crtcs[] order.
        # Track BOTH the chosen id and its bit so plane discovery can
        # filter planes via their own possible_crtcs bitmask later.
        for i, cid in enumerate(crtcs):
            if enc.possible_crtcs & (1 << i):
                self._crtc_id = cid
                self._crtc_bit = 1 << i
                break
        if self._crtc_id == 0:
            raise RuntimeError("encoder has no usable CRTC")

        # Save the original CRTC config so close() can restore it —
        # without this, exiting our process leaves the screen in
        # whatever state we left it.
        orig = _DrmModeCrtc()
        orig.crtc_id = self._crtc_id
        _ioctl(self._fd, DRM_IOCTL_MODE_GETCRTC, orig)
        self._original_crtc = orig

        self._mode = chosen_mode
        self.display_width = chosen_mode.hdisplay
        self.display_height = chosen_mode.vdisplay
        log.info(
            "DRM: connector=%d encoder=%d crtc=%d mode=%s (%dx%d)",
            self._connector_id, self._encoder_id, self._crtc_id,
            chosen_mode.name.decode(errors="ignore"),
            chosen_mode.hdisplay, chosen_mode.vdisplay,
        )

    # --- atomic / universal-planes plumbing ---

    def _set_client_caps(self) -> None:
        """Opt this DRM master into universal planes + atomic. Must run
        BEFORE GETPLANERESOURCES (else only primary planes show up) and
        BEFORE any DRM_IOCTL_MODE_ATOMIC (else EOPNOTSUPP)."""
        assert self._fd is not None
        for cap in (DRM_CLIENT_CAP_UNIVERSAL_PLANES, DRM_CLIENT_CAP_ATOMIC):
            c = _DrmSetClientCap(capability=cap, value=1)
            _ioctl(self._fd, DRM_IOCTL_SET_CLIENT_CAP, c)

    def _get_object_properties(
        self, obj_id: int, obj_type: int
    ) -> dict[str, int]:
        """Map property name → property id for the given DRM object.

        Two-call dance: first OBJ_GETPROPERTIES returns count, second
        fills the prop-id and prop-value arrays. We then GETPROPERTY on
        each id to pull the human-readable name (the kernel does not
        expose a single ioctl that gives names + ids in one shot).

        We only need name→id here; the live values are read separately
        when needed (e.g. plane "type" classification).
        """
        assert self._fd is not None
        obj = _DrmModeObjGetProperties()
        obj.obj_id = obj_id
        obj.obj_type = obj_type
        _ioctl(self._fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, obj)
        if obj.count_props == 0:
            return {}
        prop_ids = (ctypes.c_uint32 * obj.count_props)()
        prop_vals = (ctypes.c_uint64 * obj.count_props)()
        obj.props_ptr = ctypes.cast(prop_ids, ctypes.c_void_p).value or 0
        obj.prop_values_ptr = ctypes.cast(prop_vals, ctypes.c_void_p).value or 0
        _ioctl(self._fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, obj)
        # Re-read count_props from the second ioctl: kernel is allowed
        # to return a smaller count (rare, but legal). Iterating past
        # that would hit zeroed slots → GETPROPERTY with prop_id=0 → EINVAL.
        out: dict[str, int] = {}
        for i in range(obj.count_props):
            prop = _DrmModeGetProperty()
            prop.prop_id = prop_ids[i]
            _ioctl(self._fd, DRM_IOCTL_MODE_GETPROPERTY, prop)
            name = prop.name.decode("ascii", errors="ignore").rstrip("\x00")
            out[name] = prop_ids[i]
        return out

    def _get_plane_type(self, plane_id: int) -> int:
        """Read the plane's "type" property value (PRIMARY/OVERLAY/CURSOR)."""
        assert self._fd is not None
        obj = _DrmModeObjGetProperties()
        obj.obj_id = plane_id
        obj.obj_type = DRM_MODE_OBJECT_PLANE
        _ioctl(self._fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, obj)
        if obj.count_props == 0:
            return -1
        prop_ids = (ctypes.c_uint32 * obj.count_props)()
        prop_vals = (ctypes.c_uint64 * obj.count_props)()
        obj.props_ptr = ctypes.cast(prop_ids, ctypes.c_void_p).value or 0
        obj.prop_values_ptr = ctypes.cast(prop_vals, ctypes.c_void_p).value or 0
        _ioctl(self._fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, obj)
        for i in range(obj.count_props):
            prop = _DrmModeGetProperty()
            prop.prop_id = prop_ids[i]
            _ioctl(self._fd, DRM_IOCTL_MODE_GETPROPERTY, prop)
            name = prop.name.decode("ascii", errors="ignore").rstrip("\x00")
            if name == "type":
                return int(prop_vals[i])
        return -1

    def _discover_planes(self) -> None:
        """Find a primary plane bound to our chosen CRTC.

        With universal-planes enabled, GETPLANERESOURCES lists ALL planes
        (primary, overlay, cursor). We filter by possible_crtcs & our
        crtc bit, then by the plane's "type" property == PRIMARY.
        """
        assert self._fd is not None and self._crtc_bit != 0
        res = _DrmModeGetPlaneRes()
        _ioctl(self._fd, DRM_IOCTL_MODE_GETPLANERESOURCES, res)
        if res.count_planes == 0:
            raise RuntimeError("no DRM planes available")
        plane_ids = (ctypes.c_uint32 * res.count_planes)()
        res.plane_id_ptr = ctypes.cast(plane_ids, ctypes.c_void_p).value or 0
        _ioctl(self._fd, DRM_IOCTL_MODE_GETPLANERESOURCES, res)

        for pid in plane_ids:
            plane = _DrmModeGetPlane()
            plane.plane_id = pid
            _ioctl(self._fd, DRM_IOCTL_MODE_GETPLANE, plane)
            if not (plane.possible_crtcs & self._crtc_bit):
                continue
            if self._get_plane_type(pid) == DRM_PLANE_TYPE_PRIMARY:
                self._primary_plane_id = pid
                break
        if self._primary_plane_id == 0:
            raise RuntimeError(
                f"no primary plane bound to CRTC {self._crtc_id} "
                f"(crtc_bit=0x{self._crtc_bit:x})"
            )
        log.info("DRM: primary plane=%d", self._primary_plane_id)

    def _discover_overlay_plane(self) -> None:
        """Find an overlay plane bound to our CRTC that supports ARGB8888.

        Run AFTER `_discover_planes` so we can skip the primary id we
        already chose. Walks all planes again (cheap — counts are tiny)
        and filters by: not-the-primary, possible_crtcs & our crtc bit,
        type == OVERLAY, and format list contains ARGB8888 (without it
        the alpha channel can't carry into the GPU compositor).
        """
        assert self._fd is not None and self._crtc_bit != 0
        res = _DrmModeGetPlaneRes()
        _ioctl(self._fd, DRM_IOCTL_MODE_GETPLANERESOURCES, res)
        plane_ids = (ctypes.c_uint32 * res.count_planes)()
        res.plane_id_ptr = ctypes.cast(plane_ids, ctypes.c_void_p).value or 0
        _ioctl(self._fd, DRM_IOCTL_MODE_GETPLANERESOURCES, res)

        for pid in plane_ids:
            if pid == self._primary_plane_id:
                continue
            plane = _DrmModeGetPlane()
            plane.plane_id = pid
            _ioctl(self._fd, DRM_IOCTL_MODE_GETPLANE, plane)
            if not (plane.possible_crtcs & self._crtc_bit):
                continue
            if self._get_plane_type(pid) != DRM_PLANE_TYPE_OVERLAY:
                continue
            if plane.count_format_types == 0:
                continue
            # Second GETPLANE call to populate the format list.
            formats = (ctypes.c_uint32 * plane.count_format_types)()
            plane.format_type_ptr = ctypes.cast(formats, ctypes.c_void_p).value or 0
            _ioctl(self._fd, DRM_IOCTL_MODE_GETPLANE, plane)
            if DRM_FORMAT_ARGB8888 not in tuple(formats):
                continue
            self._overlay_plane_id = pid
            break
        if self._overlay_plane_id == 0:
            raise RuntimeError(
                f"no overlay plane bound to CRTC {self._crtc_id} "
                f"that supports ARGB8888"
            )
        log.info("DRM: overlay plane=%d (ARGB8888)", self._overlay_plane_id)
        self._overlay_plane_props = self._get_object_properties(
            self._overlay_plane_id, DRM_MODE_OBJECT_PLANE
        )
        required = (
            "FB_ID", "CRTC_ID",
            "SRC_X", "SRC_Y", "SRC_W", "SRC_H",
            "CRTC_X", "CRTC_Y", "CRTC_W", "CRTC_H",
        )
        missing = [n for n in required if n not in self._overlay_plane_props]
        if missing:
            raise RuntimeError(
                f"overlay plane is missing required atomic properties: {missing}"
            )

    def _allocate_overlay_framebuffer(self) -> None:
        """Second dumb buffer + DRM fb in ARGB8888, at SIGN-NATIVE dims.

        The vc4 HVS (Hardware Video Scaler) scales the overlay plane's
        source rect to its CRTC dest rect at scanout. Holding the fb
        at sign-native (e.g. 128×96 = 49 KB at ARGB8888) instead of
        display-native (8 MB at 1080p ARGB8888) cuts the per-frame
        userspace work to a near-trivial split/merge + memcpy, which
        is what makes "live-updating text over a background" actually
        viable on Pi Zero 2 W.

        Buffer is zero-initialized which (on LE arm64 with byte order
        [B,G,R,A]) means fully transparent black — so before the
        caller provides overlay content, the primary plane is what
        eyeballs see, same as before this commit.
        """
        assert self._fd is not None
        create = _DrmModeCreateDumb()
        create.width = self.width
        create.height = self.height
        create.bpp = 32
        _ioctl(self._fd, DRM_IOCTL_MODE_CREATE_DUMB, create)
        self._overlay_dumb_handle = create.handle
        self._overlay_dumb_size = int(create.size)
        self._overlay_dumb_pitch = int(create.pitch)

        fb = _DrmModeFbCmd2()
        fb.width = self.width
        fb.height = self.height
        fb.pixel_format = DRM_FORMAT_ARGB8888
        fb.handles[0] = self._overlay_dumb_handle
        fb.pitches[0] = self._overlay_dumb_pitch
        _ioctl(self._fd, DRM_IOCTL_MODE_ADDFB2, fb)
        self._overlay_fb_id = fb.fb_id

        map_dumb = _DrmModeMapDumb()
        map_dumb.handle = self._overlay_dumb_handle
        _ioctl(self._fd, DRM_IOCTL_MODE_MAP_DUMB, map_dumb)
        self._overlay_mmap = mmap.mmap(
            self._fd,
            self._overlay_dumb_size,
            mmap.MAP_SHARED,
            mmap.PROT_READ | mmap.PROT_WRITE,
            offset=int(map_dumb.offset),
        )
        # CREATE_DUMB usually zeroes the buffer, but the kernel doesn't
        # promise that across all drivers. Explicit init = transparent
        # everywhere so first-frame doesn't show driver garbage.
        self._overlay_mmap[: self._overlay_dumb_size] = b"\x00" * self._overlay_dumb_size
        log.info(
            "DRM: overlay fb_id=%d %dx%d ARGB8888 size=%d pitch=%d "
            "(HVS-scaled to %dx%d at +%d+%d)",
            self._overlay_fb_id, self.width, self.height,
            self._overlay_dumb_size, self._overlay_dumb_pitch,
            self._scaled_w, self._scaled_h,
            self._letterbox_x, self._letterbox_y,
        )

    def _discover_properties(self) -> None:
        """Cache property name→id for our CRTC, connector, and primary
        plane. The atomic commit path addresses every state change by
        these property ids; looking them up once at open beats hitting
        OBJ_GETPROPERTIES + GETPROPERTY per frame."""
        self._crtc_props = self._get_object_properties(
            self._crtc_id, DRM_MODE_OBJECT_CRTC
        )
        self._connector_props = self._get_object_properties(
            self._connector_id, DRM_MODE_OBJECT_CONNECTOR
        )
        self._primary_plane_props = self._get_object_properties(
            self._primary_plane_id, DRM_MODE_OBJECT_PLANE
        )
        # Sanity-check that the properties we'll address by name exist —
        # better to fail loud at open than throw a cryptic EINVAL from
        # the atomic commit because a property id is 0.
        required = {
            "crtc": (self._crtc_props, ("ACTIVE", "MODE_ID")),
            "connector": (self._connector_props, ("CRTC_ID",)),
            "primary plane": (
                self._primary_plane_props,
                (
                    "FB_ID", "CRTC_ID",
                    "SRC_X", "SRC_Y", "SRC_W", "SRC_H",
                    "CRTC_X", "CRTC_Y", "CRTC_W", "CRTC_H",
                ),
            ),
        }
        for label, (props, names) in required.items():
            missing = [n for n in names if n not in props]
            if missing:
                raise RuntimeError(
                    f"{label} is missing required atomic properties: {missing}"
                )

    def _discover_compositor_planes(self) -> None:
        """Find max_animated_planes ARGB8888 overlay planes bound to
        our CRTC and allocate a dumb buffer + DRM fb for each. Each
        plane is a slot for one motion-animated layer in the active
        slide; bg + static layers go on the primary plane via
        render_frame() (software composite once at slide entry).

        Each plane's fb is allocated at max sign-native dims, but
        attach_animated_layer's SRC_W/H is set to the layer's glyph-
        bbox subset — vc4 LBM consumption scales with SRC_W not fb
        width, so smaller glyph bboxes mean more simultaneous animated
        layers within the LBM ceiling (~3 planes total at 1080p
        full-frame source; more at smaller source widths).

        Planes are NOT bound to the CRTC at this point — FB_ID and
        CRTC_ID stay 0 until attach_animated_layer + commit() activate
        them. This lets us pre-allocate the whole plane stack once at
        __enter__ without affecting what's on screen.
        """
        n_needed = self.max_animated_planes
        if n_needed == 0:
            return
        res = _DrmModeGetPlaneRes()
        _ioctl(self._fd, DRM_IOCTL_MODE_GETPLANERESOURCES, res)
        plane_ids_arr = (ctypes.c_uint32 * res.count_planes)()
        res.plane_id_ptr = ctypes.cast(plane_ids_arr, ctypes.c_void_p).value or 0
        _ioctl(self._fd, DRM_IOCTL_MODE_GETPLANERESOURCES, res)

        skip = {self._primary_plane_id}
        if self._overlay_plane_id:
            skip.add(self._overlay_plane_id)
        chosen: list[int] = []
        for pid in plane_ids_arr:
            if len(chosen) >= n_needed:
                break
            if pid in skip:
                continue
            plane = _DrmModeGetPlane()
            plane.plane_id = pid
            _ioctl(self._fd, DRM_IOCTL_MODE_GETPLANE, plane)
            if not (plane.possible_crtcs & self._crtc_bit):
                continue
            if self._get_plane_type(pid) != DRM_PLANE_TYPE_OVERLAY:
                continue
            if plane.count_format_types == 0:
                continue
            formats = (ctypes.c_uint32 * plane.count_format_types)()
            plane.format_type_ptr = ctypes.cast(formats, ctypes.c_void_p).value or 0
            _ioctl(self._fd, DRM_IOCTL_MODE_GETPLANE, plane)
            if DRM_FORMAT_ARGB8888 not in tuple(formats):
                continue
            chosen.append(pid)
        if len(chosen) < n_needed:
            raise RuntimeError(
                f"GPU compositor needs {n_needed} ARGB8888 overlay "
                f"planes on CRTC {self._crtc_id}, found {len(chosen)}. "
                f"Lower max_animated_planes or pick a CRTC with more "
                f"overlays."
            )

        self._animated_planes = [
            self._allocate_compositor_plane_slot(pid) for pid in chosen
        ]
        log.info(
            "DRM: GPU compositor allocated %d animated planes (ids=%s)",
            len(self._animated_planes),
            [s.plane_id for s in self._animated_planes],
        )

    def _allocate_compositor_plane_slot(self, plane_id: int) -> _PlaneSlot:
        """Allocate dumb buffer + DRM fb in ARGB8888 at sign-native
        dims, mmap, fetch atomic property ids. Returns a _PlaneSlot
        ready for attach via update_animated_layer / set_static_text_
        bitmap. Plane stays disabled (CRTC_ID=0) until the caller
        commits an activation."""
        assert self._fd is not None
        create = _DrmModeCreateDumb()
        create.width = self.width
        create.height = self.height
        create.bpp = 32  # ARGB8888
        _ioctl(self._fd, DRM_IOCTL_MODE_CREATE_DUMB, create)

        fb = _DrmModeFbCmd2()
        fb.width = self.width
        fb.height = self.height
        fb.pixel_format = DRM_FORMAT_ARGB8888
        fb.handles[0] = create.handle
        fb.pitches[0] = create.pitch
        _ioctl(self._fd, DRM_IOCTL_MODE_ADDFB2, fb)

        map_dumb = _DrmModeMapDumb()
        map_dumb.handle = create.handle
        _ioctl(self._fd, DRM_IOCTL_MODE_MAP_DUMB, map_dumb)
        mm = mmap.mmap(
            self._fd,
            int(create.size),
            mmap.MAP_SHARED,
            mmap.PROT_READ | mmap.PROT_WRITE,
            offset=int(map_dumb.offset),
        )
        # Zero-init so the first scanout (when the plane gets bound)
        # doesn't show driver garbage.
        mm[: int(create.size)] = b"\x00" * int(create.size)

        props = self._get_object_properties(plane_id, DRM_MODE_OBJECT_PLANE)
        required = (
            "FB_ID", "CRTC_ID",
            "SRC_X", "SRC_Y", "SRC_W", "SRC_H",
            "CRTC_X", "CRTC_Y", "CRTC_W", "CRTC_H",
        )
        missing = [n for n in required if n not in props]
        if missing:
            raise RuntimeError(
                f"compositor plane {plane_id} missing required props: {missing}"
            )
        # alpha + zpos + pixel-blend-mode are optional but expected on
        # vc4 (probe confirmed 2026-05-02). Log if they're absent so a
        # future kernel regression surfaces loudly.
        for opt in ("alpha", "zpos", "pixel blend mode"):
            if opt not in props:
                log.warning(
                    "DRM: compositor plane %d missing optional %r prop",
                    plane_id, opt,
                )

        return _PlaneSlot(
            plane_id=plane_id,
            props=props,
            fb_id=fb.fb_id,
            dumb_handle=create.handle,
            dumb_size=int(create.size),
            dumb_pitch=int(create.pitch),
            mmap=mm,
            width=self.width,
            height=self.height,
            attached=False,
        )

    def _create_mode_blob(self, mode: _DrmModeInfo) -> int:
        """Stash a struct drm_mode_modeinfo in a kernel-side property
        blob and return its id. The atomic CRTC.MODE_ID property takes
        a blob id, not the struct directly."""
        assert self._fd is not None
        req = _DrmModeCreateBlob()
        req.data = ctypes.addressof(mode)
        req.length = ctypes.sizeof(_DrmModeInfo)
        _ioctl(self._fd, DRM_IOCTL_MODE_CREATEPROPBLOB, req)
        return req.blob_id

    def _atomic_commit(
        self,
        *,
        flags: int,
        object_props: list[tuple[int, list[tuple[int, int]]]],
    ) -> None:
        """Submit a flat list of (object_id, [(prop_id, value), ...])
        as a single atomic commit. Layout follows drm_mode.h's
        struct drm_mode_atomic — three parallel flat arrays plus a
        per-object property-count array.

        Memory note: the ctypes arrays MUST stay live until the ioctl
        returns. We hold them in locals here, which is sufficient (the
        ioctl is synchronous).
        """
        assert self._fd is not None
        n_objs = len(object_props)
        objs = (ctypes.c_uint32 * n_objs)(
            *[obj_id for obj_id, _ in object_props]
        )
        counts = (ctypes.c_uint32 * n_objs)(
            *[len(props) for _, props in object_props]
        )
        flat_pids: list[int] = []
        flat_vals: list[int] = []
        for _, props in object_props:
            for pid, val in props:
                flat_pids.append(pid)
                flat_vals.append(val)
        pids = (ctypes.c_uint32 * len(flat_pids))(*flat_pids)
        vals = (ctypes.c_uint64 * len(flat_vals))(*flat_vals)

        req = _DrmModeAtomic()
        req.flags = flags
        req.count_objs = n_objs
        req.objs_ptr = ctypes.cast(objs, ctypes.c_void_p).value or 0
        req.count_props_ptr = ctypes.cast(counts, ctypes.c_void_p).value or 0
        req.props_ptr = ctypes.cast(pids, ctypes.c_void_p).value or 0
        req.prop_values_ptr = ctypes.cast(vals, ctypes.c_void_p).value or 0
        _ioctl(self._fd, DRM_IOCTL_MODE_ATOMIC, req)

    def _atomic_modeset(self) -> None:
        """Atomic mode-set — bind primary (and optional overlay) planes
        to the CRTC at the discovered mode.

        Both planes use HVS plane-scaling: their fb is at sign-native
        dims (e.g. 128×96), SRC is the full sign rect in 16.16 fp, and
        CRTC dest is the letterboxed display region. The vc4 HVS scales
        each plane to its CRTC rect at scanout, so per-frame software
        cost is just a sign-native swizzle + tiny memcpy. Areas of the
        CRTC outside the letterboxed region (the black bands) are
        uncovered by any plane; the HVS programs the CRTC's background
        register (default black on vc4) for those scanlines.
        """
        assert (
            self._fd is not None
            and self._mode is not None
            and self._fb_id != 0
            and self._primary_plane_id != 0
        )
        self._mode_blob_id = self._create_mode_blob(self._mode)
        cp = self._crtc_props
        np_ = self._connector_props
        pp = self._primary_plane_props
        sign_src_w = self.width << 16
        sign_src_h = self.height << 16
        object_props: list[tuple[int, list[tuple[int, int]]]] = [
            (self._crtc_id, [
                (cp["ACTIVE"], 1),
                (cp["MODE_ID"], self._mode_blob_id),
            ]),
            (self._connector_id, [
                (np_["CRTC_ID"], self._crtc_id),
            ]),
            (self._primary_plane_id, [
                (pp["FB_ID"], self._fb_id),
                (pp["CRTC_ID"], self._crtc_id),
                (pp["SRC_X"], 0),
                (pp["SRC_Y"], 0),
                (pp["SRC_W"], sign_src_w),
                (pp["SRC_H"], sign_src_h),
                (pp["CRTC_X"], self._letterbox_x),
                (pp["CRTC_Y"], self._letterbox_y),
                (pp["CRTC_W"], self._scaled_w),
                (pp["CRTC_H"], self._scaled_h),
            ]),
        ]
        if self.enable_overlay:
            op = self._overlay_plane_props
            # Overlay shares the same SRC and CRTC rects as primary —
            # both are sign-native scaled to the same letterboxed
            # region, so overlay alpha-pixels land directly on top of
            # primary content pixels at scanout (no sub-pixel drift).
            object_props.append((self._overlay_plane_id, [
                (op["FB_ID"], self._overlay_fb_id),
                (op["CRTC_ID"], self._crtc_id),
                (op["SRC_X"], 0),
                (op["SRC_Y"], 0),
                (op["SRC_W"], sign_src_w),
                (op["SRC_H"], sign_src_h),
                (op["CRTC_X"], self._letterbox_x),
                (op["CRTC_Y"], self._letterbox_y),
                (op["CRTC_W"], self._scaled_w),
                (op["CRTC_H"], self._scaled_h),
            ]))
        self._atomic_commit(
            flags=DRM_MODE_ATOMIC_ALLOW_MODESET,
            object_props=object_props,
        )
        log.info(
            "DRM: atomic mode-set committed (%s, HVS-scaled to %dx%d at +%d+%d)",
            "primary + overlay" if self.enable_overlay else "primary only",
            self._scaled_w, self._scaled_h,
            self._letterbox_x, self._letterbox_y,
        )

    # --- framebuffer alloc + map ---

    def _alloc_dumb_primary_fb(
        self,
    ) -> tuple[int, int, mmap.mmap, int, int]:
        """Allocate a sign-native dumb buffer + register as DRM fb +
        mmap. Used both for the default primary buffer and per-slide
        cached buffers in the pool (#218 part 2). Returns (fb_id,
        dumb_handle, mmap, size, pitch). Caller is responsible for
        cleanup via _destroy_dumb_primary_fb."""
        assert self._fd is not None
        create = _DrmModeCreateDumb()
        create.width = self.width
        create.height = self.height
        create.bpp = self._bytes_per_pixel * 8
        _ioctl(self._fd, DRM_IOCTL_MODE_CREATE_DUMB, create)
        dumb_handle = create.handle
        dumb_size = int(create.size)
        dumb_pitch = int(create.pitch)
        try:
            fb = _DrmModeFbCmd2()
            fb.width = self.width
            fb.height = self.height
            fb.pixel_format = self._drm_format
            fb.handles[0] = dumb_handle
            fb.pitches[0] = dumb_pitch
            _ioctl(self._fd, DRM_IOCTL_MODE_ADDFB2, fb)
            fb_id = fb.fb_id
            try:
                map_dumb = _DrmModeMapDumb()
                map_dumb.handle = dumb_handle
                _ioctl(self._fd, DRM_IOCTL_MODE_MAP_DUMB, map_dumb)
                buf_mmap = mmap.mmap(
                    self._fd,
                    dumb_size,
                    mmap.MAP_SHARED,
                    mmap.PROT_READ | mmap.PROT_WRITE,
                    offset=int(map_dumb.offset),
                )
            except Exception:
                fb_id_c = ctypes.c_uint32(fb_id)
                _ioctl(self._fd, DRM_IOCTL_MODE_RMFB, fb_id_c)
                raise
        except Exception:
            d = _DrmModeDestroyDumb()
            d.handle = dumb_handle
            _ioctl(self._fd, DRM_IOCTL_MODE_DESTROY_DUMB, d)
            raise
        return fb_id, dumb_handle, buf_mmap, dumb_size, dumb_pitch

    def _destroy_dumb_primary_fb(
        self, fb_id: int, dumb_handle: int, buf_mmap: mmap.mmap | None,
    ) -> None:
        """Tear down a dumb-buffer-backed primary fb: mmap.close ->
        RmFB -> DESTROY_DUMB. Each leg guarded -- a partially-
        allocated buffer (e.g. _alloc_dumb_primary_fb threw mid-way)
        still cleans up cleanly."""
        if self._fd is None:
            return
        try:
            if buf_mmap is not None:
                buf_mmap.close()
        except Exception:
            log.exception("DRMRenderer: dumb buffer mmap close failed")
        try:
            if fb_id:
                fb_id_c = ctypes.c_uint32(fb_id)
                _ioctl(self._fd, DRM_IOCTL_MODE_RMFB, fb_id_c)
        except OSError:
            log.exception("DRMRenderer: dumb buffer RmFB failed")
        try:
            if dumb_handle:
                d = _DrmModeDestroyDumb()
                d.handle = dumb_handle
                _ioctl(self._fd, DRM_IOCTL_MODE_DESTROY_DUMB, d)
        except OSError:
            log.exception("DRMRenderer: DESTROY_DUMB failed")

    def _allocate_framebuffer(self) -> None:
        """Create the DEFAULT primary dumb buffer at sign-native dims.
        This is the always-allocated buffer for render_frame() / fallback
        paths (welcome screen, stream takeover, MockRenderer-style
        per-frame painting). Per-slide cached buffers (#218) are
        separate -- see prepare_primary_buffer / stage_primary_buffer.

        The vc4 HVS scales this fb to the letterboxed CRTC region at
        scanout; per-frame work is just a sign-native swizzle + tiny
        memcpy (e.g. 24 KB at 128×96 RGB565 vs the 4 MB / display-
        native fb the prior implementation wrote).
        """
        assert self._fd is not None and self._mode is not None
        fb_id, dumb_handle, buf_mmap, dumb_size, dumb_pitch = (
            self._alloc_dumb_primary_fb()
        )
        self._fb_id = fb_id
        self._dumb_handle = dumb_handle
        self._mmap = buf_mmap
        self._dumb_size = dumb_size
        self._dumb_pitch = dumb_pitch
        log.info(
            "DRM: primary fb_id=%d %dx%d %s size=%d pitch=%d "
            "(HVS-scaled to %dx%d at +%d+%d)",
            self._fb_id, self.width, self.height, self.pixel_format,
            self._dumb_size, self._dumb_pitch,
            self._scaled_w, self._scaled_h,
            self._letterbox_x, self._letterbox_y,
        )

    # --- close ---

    @property
    def drm_fd(self) -> int | None:
        """The active DRM file descriptor, or None if not opened.
        Exposed so a peer renderer (e.g. ShaderRenderer for a transition
        window) can issue ioctls under THIS renderer's master without
        re-opening the device or fighting for master. The peer must
        not close this fd; that stays our responsibility."""
        return self._fd

    def restage_primary_fb(self) -> None:
        """Re-stage the primary plane's FB_ID + CRTC binding so the
        next `commit()` atomic-rebinds OUR primary fb to the CRTC.

        Required after a peer renderer (e.g. ShaderRenderer for a
        transition window) has driven the primary plane via legacy
        drmModeSetCrtc with its own GBM-backed fbs. By the time the
        peer closes, the kernel implicitly pins its last fb to the
        CRTC; without this restage, our subsequent `commit()` runs
        through `_pending_props` empty and the screen keeps showing
        the peer's last frame indefinitely (until the next mode-set
        or any other primary-FB_ID-touching atomic commit).

        Caller order for a clean handoff back to multi-plane:
          drm.render_frame(new_bytes)   # paint our dumb buffer
          drm.restage_primary_fb()      # stage primary FB_ID
          drm.commit()                  # atomic rebind
          shader.close()                # peer RmFB's a now-idle fb
        """
        pp = self._primary_plane_props
        sign_src_w = self.width << 16
        sign_src_h = self.height << 16
        pid = self._primary_plane_id
        # Mirrors the property set in _commit_initial_modeset for
        # primary; matches whatever HVS scaling the renderer was
        # constructed for (sign-native source, letterboxed CRTC dest).
        self._pending_props[(pid, pp["FB_ID"])] = self._fb_id
        self._pending_props[(pid, pp["CRTC_ID"])] = self._crtc_id
        self._pending_props[(pid, pp["SRC_X"])] = 0
        self._pending_props[(pid, pp["SRC_Y"])] = 0
        self._pending_props[(pid, pp["SRC_W"])] = sign_src_w
        self._pending_props[(pid, pp["SRC_H"])] = sign_src_h
        self._pending_props[(pid, pp["CRTC_X"])] = self._letterbox_x
        self._pending_props[(pid, pp["CRTC_Y"])] = self._letterbox_y
        self._pending_props[(pid, pp["CRTC_W"])] = self._scaled_w
        self._pending_props[(pid, pp["CRTC_H"])] = self._scaled_h

    def close(self) -> None:
        if self._fd is None:
            return
        # Best-effort cleanup; log but don't raise. Note: EINVAL when
        # restoring the original CRTC is common on first-time DRM
        # masters because the saved CRTC was empty (no fb, no mode);
        # the kernel reaps DRM master at process exit and the console
        # comes back regardless, so swallow EINVAL silently.
        try:
            if self._original_crtc is not None and self._fd is not None:
                _ioctl(self._fd, DRM_IOCTL_MODE_SETCRTC, self._original_crtc)
        except OSError as exc:
            if exc.errno != 22:  # EINVAL
                log.exception("DRMRenderer: failed to restore original CRTC")
        # Per-slide primary buffer pool cleanup (#218 part 2). Tear
        # down each cached slide's dumb buffer + fb + mmap. Done
        # BEFORE the default primary teardown so RmFB ordering is
        # consistent (kernel doesn't care, but tidier).
        try:
            while self._primary_buffer_pool:
                _, entry = self._primary_buffer_pool.popitem(last=True)
                fb_id, dumb_handle, buf_mmap, _version = entry
                self._destroy_dumb_primary_fb(
                    fb_id, dumb_handle, buf_mmap,
                )
        except Exception:
            log.exception("DRMRenderer: primary buffer pool teardown failed")
        try:
            if self._mmap is not None:
                self._mmap.close()
                self._mmap = None
        except Exception:
            log.exception("DRMRenderer: mmap close failed")
        try:
            if self._fb_id:
                fb_id = ctypes.c_uint32(self._fb_id)
                _ioctl(self._fd, DRM_IOCTL_MODE_RMFB, fb_id)
                self._fb_id = 0
        except OSError:
            log.exception("DRMRenderer: rmfb failed")
        try:
            if self._dumb_handle:
                d = _DrmModeDestroyDumb()
                d.handle = self._dumb_handle
                _ioctl(self._fd, DRM_IOCTL_MODE_DESTROY_DUMB, d)
                self._dumb_handle = 0
        except OSError:
            log.exception("DRMRenderer: destroy dumb failed")
        # Overlay-plane resources mirror the primary cleanup pattern:
        # mmap close → RMFB → DESTROY_DUMB. Each leg guarded so a
        # partially-initialized renderer (e.g. enable_overlay open
        # failed mid-allocation) still cleans up everything it owns.
        try:
            if self._overlay_mmap is not None:
                self._overlay_mmap.close()
                self._overlay_mmap = None
        except Exception:
            log.exception("DRMRenderer: overlay mmap close failed")
        try:
            if self._overlay_fb_id:
                fb_id = ctypes.c_uint32(self._overlay_fb_id)
                _ioctl(self._fd, DRM_IOCTL_MODE_RMFB, fb_id)
                self._overlay_fb_id = 0
        except OSError:
            log.exception("DRMRenderer: overlay rmfb failed")
        try:
            if self._overlay_dumb_handle:
                d = _DrmModeDestroyDumb()
                d.handle = self._overlay_dumb_handle
                _ioctl(self._fd, DRM_IOCTL_MODE_DESTROY_DUMB, d)
                self._overlay_dumb_handle = 0
        except OSError:
            log.exception("DRMRenderer: overlay destroy dumb failed")
        # GPU-compositor planes (max_animated_planes > 0): tear down
        # every animated plane in the same mmap → RMFB → DESTROY_DUMB
        # order the legacy single-overlay path uses.
        for slot in self._animated_planes:
            try:
                if slot.mmap is not None:
                    slot.mmap.close()
                    slot.mmap = None
            except Exception:
                log.exception(
                    "DRMRenderer: compositor plane %d mmap close failed",
                    slot.plane_id,
                )
            try:
                if slot.fb_id:
                    fb_id = ctypes.c_uint32(slot.fb_id)
                    _ioctl(self._fd, DRM_IOCTL_MODE_RMFB, fb_id)
                    slot.fb_id = 0
            except OSError:
                log.exception(
                    "DRMRenderer: compositor plane %d rmfb failed",
                    slot.plane_id,
                )
            try:
                if slot.dumb_handle:
                    d = _DrmModeDestroyDumb()
                    d.handle = slot.dumb_handle
                    _ioctl(self._fd, DRM_IOCTL_MODE_DESTROY_DUMB, d)
                    slot.dumb_handle = 0
            except OSError:
                log.exception(
                    "DRMRenderer: compositor plane %d destroy dumb failed",
                    slot.plane_id,
                )
        self._animated_planes = []
        self._pending_props.clear()
        try:
            if self._mode_blob_id:
                blob = _DrmModeDestroyBlob()
                blob.blob_id = self._mode_blob_id
                _ioctl(self._fd, DRM_IOCTL_MODE_DESTROYPROPBLOB, blob)
                self._mode_blob_id = 0
        except OSError:
            log.exception("DRMRenderer: destroy mode blob failed")
        try:
            os.close(self._fd)
        except OSError:
            pass
        finally:
            self._fd = None

    # --- render path ---

    def encode_native_payload(self, frame: bytes) -> bytes:
        """Convert RGB888 `frame` to the renderer's native pixel format
        bytes (BGR;16 for rgb565, [B,G,R,X] for xrgb8888). Pure CPU
        work; no I/O, no global state. Splitting this out from
        render_frame lets callers cache the result across slide
        re-attaches (#218): the conversion is content-dependent only
        (same RGB in -> same native bytes out), and PlaybackLoop's
        prerender thread can run it BEFORE the slide actually
        attaches so render_frame_native (just the mmap memcpy) is
        the only critical-path work at the seam.

        ~30-70 ms at 1080p on Pi Zero 2 W; all the time saved here is
        time the user doesn't see the screen frozen at the seam."""
        expected = self.width * self.height * 3
        if len(frame) != expected:
            raise ValueError(
                f"frame length {len(frame)} != {self.width}x{self.height} RGB888 ({expected})"
            )
        image = Image.frombytes("RGB", (self.width, self.height), frame)
        if self.pixel_format == "rgb565":
            # Pillow's deprecated "BGR;16" mode = RGB565 little-endian
            # via a C-side per-pixel pack. numpy fallback covers
            # Pillow 12's removal slated for 2025-10-15.
            import warnings
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    return image.convert("BGR;16").tobytes()
            except (ValueError, OSError):
                arr = np.frombuffer(image.tobytes(), dtype=np.uint8).reshape(
                    image.height, image.width, 3
                ).astype(np.uint16, copy=False)
                packed = (
                    ((arr[..., 0] & 0xF8) << 8)
                    | ((arr[..., 1] & 0xFC) << 3)
                    | (arr[..., 2] >> 3)
                )
                return packed.astype("<u2").tobytes()
        # XRGB8888 — bytes [B, G, R, X] per pixel on LE arm64.
        r, g, b = image.split()
        alpha = Image.new("L", image.size, 255)
        return Image.merge("RGBA", (b, g, r, alpha)).tobytes()

    def render_frame_native(self, payload: bytes) -> None:
        """Memcpy pre-encoded native-format bytes into the primary
        plane's mmap. Bypasses the PIL conversion in render_frame --
        callers that have a cached native payload (from
        encode_native_payload) skip ~30-70 ms of work at slide attach
        (#218). No-op if the renderer hasn't opened yet."""
        if self._mmap is None:
            raise RuntimeError("DRMRenderer not opened")
        # mmap slice-assignment is a single C-level memcpy. If the
        # kernel padded the row pitch beyond width*bpp (rare at small
        # dims, but possible), fall back to per-row copy.
        row_bytes = self.width * self._bytes_per_pixel
        if self._dumb_pitch == row_bytes:
            self._mmap[: len(payload)] = payload
        else:
            for y in range(self.height):
                start = y * row_bytes
                self._mmap[
                    y * self._dumb_pitch : y * self._dumb_pitch + row_bytes
                ] = payload[start : start + row_bytes]

    def render_frame(self, frame: bytes) -> None:
        """Convert RGB888 `frame` to the configured pixel format and
        write to the sign-native primary mmap. Combines
        encode_native_payload + render_frame_native; callers that
        want to cache the encoded bytes should call those directly."""
        self.render_frame_native(self.encode_native_payload(frame))

    # --- per-slide primary buffer pool (#218 part 2) ---
    #
    # Each cached slide gets its own dumb buffer + fb_id + mmap, pre-
    # painted in renderer-native format during steady-state. At slide
    # attach we just stage FB_ID = pool[slide_id].fb_id in the atomic
    # commit -- zero memcpy on the seam, only the kernel's atomic
    # commit + vblank wait remain. The painting (encode + write into
    # the slide's mmap) happens off the asyncio main thread via
    # PlaybackLoop's prerender to_thread tasks, so even the encoding
    # cost is invisible to the user.

    def has_primary_buffer(self, key: object) -> bool:
        """True if a per-slide primary buffer for `key` is already
        allocated + painted. Caller (compositor.attach) checks this
        to decide between pool fast path and the legacy
        render_frame_native fallback."""
        return key in self._primary_buffer_pool

    def prepare_primary_buffer(
        self,
        key: object,
        rgb_bytes: bytes,
        *,
        content_version: object = None,
    ) -> None:
        """Allocate a dumb buffer + fb for `key` (idempotent) and
        encode rgb_bytes directly into its mmap. Safe to call from a
        worker thread (it's pure CPU work + an mmap memcpy that's
        unrelated to the active scanout). At slide attach time, the
        buffer is already painted -- just stage_primary_buffer flips
        FB_ID, no encode + memcpy on the critical path.

        Soft-caps the pool at _max_pool_buffers (20 by default); LRU-
        evicts the least-recently-attached slide when over cap. Eviction
        cost is O(1) ioctls (RmFB + DESTROY_DUMB).

        Idempotent only when `content_version` matches the cached
        version. If `content_version` differs (e.g. slide.updated_at
        moved), the cached buffer's mmap is repainted with the new
        bytes -- preserves fb_id but updates pixels, so any
        outstanding stage_primary_buffer references stay valid."""
        if self._fd is None:
            raise RuntimeError("DRMRenderer not opened")
        existing = self._primary_buffer_pool.get(key)
        if existing is not None:
            ex_fb_id, ex_dumb, ex_mmap, ex_version = existing
            if ex_version == content_version:
                # Content unchanged; mark MRU and skip.
                self._primary_buffer_pool.move_to_end(key)
                return
            # Content changed (slide.updated_at moved): repaint the
            # SAME buffer in place. Don't release+realloc -- that would
            # change fb_id and break any in-flight atomic commit.
            try:
                payload = self.encode_native_payload(rgb_bytes)
                row_bytes = self.width * self._bytes_per_pixel
                if self._dumb_pitch == row_bytes:
                    ex_mmap[: len(payload)] = payload
                else:
                    for y in range(self.height):
                        start = y * row_bytes
                        ex_mmap[
                            y * self._dumb_pitch : y * self._dumb_pitch + row_bytes
                        ] = payload[start : start + row_bytes]
                self._primary_buffer_pool[key] = (
                    ex_fb_id, ex_dumb, ex_mmap, content_version,
                )
                self._primary_buffer_pool.move_to_end(key)
                # 16.5 / sweep #8 A6: per-slide pool ops are useful at
                # DEBUG for debugging a specific repaint, but spam
                # journalctl at INFO during normal playback (one entry
                # per slide change). Init-time DRM INFO lines stay.
                log.debug(
                    "DRM: pool repainted slide %s fb_id=%d "
                    "(updated_at changed)",
                    key, ex_fb_id,
                )
            except Exception:
                log.exception(
                    "DRM: pool repaint failed for slide %s; "
                    "buffer left in stale state",
                    key,
                )
            return
        # Evict LRU until under cap. (We're about to add one entry.)
        while len(self._primary_buffer_pool) >= self._max_pool_buffers:
            evict_key, evict_buf = (
                self._primary_buffer_pool.popitem(last=False)
            )
            evict_fb_id, evict_dumb_handle, evict_mmap, _ = evict_buf
            self._destroy_dumb_primary_fb(
                evict_fb_id, evict_dumb_handle, evict_mmap,
            )
            log.debug(
                "DRM: pool LRU-evicted slide %r (fb_id=%d)",
                evict_key, evict_fb_id,
            )
        # Allocate + encode.
        fb_id, dumb_handle, buf_mmap, _, dumb_pitch = (
            self._alloc_dumb_primary_fb()
        )
        try:
            payload = self.encode_native_payload(rgb_bytes)
            row_bytes = self.width * self._bytes_per_pixel
            if dumb_pitch == row_bytes:
                buf_mmap[: len(payload)] = payload
            else:
                for y in range(self.height):
                    start = y * row_bytes
                    buf_mmap[
                        y * dumb_pitch : y * dumb_pitch + row_bytes
                    ] = payload[start : start + row_bytes]
        except Exception:
            # Encode/write failed; tear down the half-prepared buffer
            # so we don't leak a kernel handle.
            self._destroy_dumb_primary_fb(fb_id, dumb_handle, buf_mmap)
            raise
        self._primary_buffer_pool[key] = (
            fb_id, dumb_handle, buf_mmap, content_version,
        )
        # 16.5 / sweep #8 A6: see pool-repaint note above; same demote.
        log.debug(
            "DRM: pool added slide %s fb_id=%d (pool size=%d/%d)",
            key, fb_id,
            len(self._primary_buffer_pool), self._max_pool_buffers,
        )

    def release_primary_buffer(self, key: object) -> bool:
        """Free `key`'s pool buffer. Returns True if freed, False if
        not present. Called when a slide leaves the playlist
        (updated_at-driven content refresh repaints in place via
        prepare_primary_buffer instead, preserving fb_id)."""
        entry = self._primary_buffer_pool.pop(key, None)
        if entry is None:
            return False
        fb_id, dumb_handle, buf_mmap, _ = entry
        self._destroy_dumb_primary_fb(fb_id, dumb_handle, buf_mmap)
        return True

    def stage_primary_buffer(self, key: object) -> bool:
        """Stage atomic commit FB_ID = pool[key].fb_id (and primary
        plane CRTC rects). Returns True if staged, False if `key` not
        in pool (caller falls back to render_frame_native + restage_
        primary_fb path). Updates LRU."""
        if key not in self._primary_buffer_pool:
            return False
        fb_id, _, _, _ = self._primary_buffer_pool[key]
        self._primary_buffer_pool.move_to_end(key)
        # Stage primary plane: FB_ID + CRTC binding + sign-source rect
        # + letterboxed dest rect. Mirrors restage_primary_fb but with
        # the pool buffer's fb_id, not the default _fb_id.
        if self._primary_plane_id == 0:
            log.warning(
                "DRMRenderer: primary plane not configured; "
                "stage_primary_buffer is a no-op",
            )
            return False
        pid = self._primary_plane_id
        pp = self._primary_plane_props
        sign_src_w = self.width << 16
        sign_src_h = self.height << 16
        self._pending_props[(pid, pp["FB_ID"])] = fb_id
        self._pending_props[(pid, pp["CRTC_ID"])] = self._crtc_id
        self._pending_props[(pid, pp["SRC_X"])] = 0
        self._pending_props[(pid, pp["SRC_Y"])] = 0
        self._pending_props[(pid, pp["SRC_W"])] = sign_src_w
        self._pending_props[(pid, pp["SRC_H"])] = sign_src_h
        self._pending_props[(pid, pp["CRTC_X"])] = self._letterbox_x
        self._pending_props[(pid, pp["CRTC_Y"])] = self._letterbox_y
        self._pending_props[(pid, pp["CRTC_W"])] = self._scaled_w
        self._pending_props[(pid, pp["CRTC_H"])] = self._scaled_h
        return True

    # --- GPU-compositor public API ---

    def attach_animated_layer(
        self,
        slot_idx: int,
        rgba_bytes: bytes,
        *,
        src_w: int,
        src_h: int,
        crtc_x: int,
        crtc_y: int,
        crtc_w: int,
        crtc_h: int,
        zpos: int | None = None,
    ) -> None:
        """Bind animated overlay plane `slot_idx` to our CRTC.

        rgba_bytes: src_w * src_h * 4 bytes — the layer's content
            cropped to its glyph bounding box. Caller is responsible
            for the crop; smaller bbox = less vc4 LBM consumed = more
            animated layers fit simultaneously.
        src_w / src_h: pixel dims of the rgba buffer.
        crtc_x / crtc_y / crtc_w / crtc_h: where this layer sits on
            the display, in display pixels. crtc_w / crtc_h can equal
            src_w / src_h (1:1 paint) or differ (HVS scales). For
            breathe and per-frame scale, just keep updating these
            via update_animated_layer.

        zpos defaults to 2 + slot_idx (animated layers stack above
        primary in slot order). Caller must `commit()`.
        """
        slot = self._require_animated_slot(slot_idx)
        if src_w <= 0 or src_h <= 0:
            raise ValueError(f"src_w/src_h must be positive, got {src_w}x{src_h}")
        if src_w > slot.width or src_h > slot.height:
            raise ValueError(
                f"src dims {src_w}x{src_h} exceed plane fb {slot.width}x{slot.height}"
            )
        self._write_plane_buffer_subregion(slot, rgba_bytes, src_w, src_h)
        if zpos is None:
            zpos = 2 + slot_idx
        # Force PREMULTI blend mode explicitly. vc4's default IS
        # PREMULTI but the property persists across DRM master
        # sessions — if a previous run staged COVERAGE on this plane,
        # not setting it here leaves the leftover COVERAGE active. Set
        # it every attach to make state deterministic. PREMULTI under
        # premultiplied RGB input (handled by _write_plane_buffer_
        # subregion) honors per-pixel alpha correctly, so transparent
        # bbox pixels stay transparent and plane.alpha cleanly fades
        # the layer.
        self._stage_plane_props(
            slot,
            fb_id=slot.fb_id,
            crtc_id=self._crtc_id,
            src_x=0, src_y=0,
            src_w=src_w << 16, src_h=src_h << 16,
            crtc_x=crtc_x, crtc_y=crtc_y,
            crtc_w=crtc_w, crtc_h=crtc_h,
            alpha=65535,
            zpos=zpos,
            pixel_blend_mode=DRM_MODE_BLEND_PREMULTI,
        )
        slot.attached = True

    def detach_animated_layer(self, slot_idx: int) -> None:
        """Disable the animated plane at `slot_idx` (CRTC_ID=0,
        FB_ID=0). The dumb buffer stays allocated for the next attach.
        Caller must `commit()`."""
        slot = self._require_animated_slot(slot_idx)
        self._stage_plane_detach(slot)
        slot.attached = False

    def update_animated_layer(
        self,
        slot_idx: int,
        *,
        crtc_x: int | None = None,
        crtc_y: int | None = None,
        crtc_w: int | None = None,
        crtc_h: int | None = None,
        src_x: int | None = None,
        src_y: int | None = None,
        src_w: int | None = None,
        src_h: int | None = None,
        alpha: int | None = None,
        zpos: int | None = None,
    ) -> None:
        """Stage per-property changes for the animated plane at `slot_
        idx`. Each kwarg is the new value or None to leave unchanged.
        All geometry kwargs (crtc_*, src_*) are in INTEGER PIXELS;
        src_x/y/w/h get the 16.16 fp shift applied internally for
        consistency with attach_animated_layer. alpha is 0-65535.
        Caller must `commit()`.

        Per-frame motion is one of:
            ticker / shake → crtc_x (and crtc_y for shake)
            bounce         → crtc_y
            breathe        → crtc_w + crtc_h + crtc_x + crtc_y
                             (orbit-around-box-center; orchestrator
                             handles the math)
            pulse          → alpha
            blink          → alpha (0 or 65535) or detach via
                             detach_animated_layer + commit
        """
        slot = self._require_animated_slot(slot_idx)
        # Consistent units: SRC_* are passed in pixels and shifted to
        # 16.16 fp here, matching attach_animated_layer's pixel-in
        # contract. Without this, a caller animating SRC_X for a
        # ticker wrap would get a 65536× off result.
        self._stage_plane_props(
            slot,
            crtc_x=crtc_x, crtc_y=crtc_y,
            crtc_w=crtc_w, crtc_h=crtc_h,
            src_x=(src_x << 16) if src_x is not None else None,
            src_y=(src_y << 16) if src_y is not None else None,
            src_w=(src_w << 16) if src_w is not None else None,
            src_h=(src_h << 16) if src_h is not None else None,
            alpha=alpha, zpos=zpos,
        )

    def commit(self, *, nonblock: bool = False) -> None:
        """Flush all staged property changes via one DRM_IOCTL_MODE_
        ATOMIC. Per-frame hot path: this is the only kernel call. No
        ALLOW_MODESET — only flips plane state, not CRTC mode.

        With nonblock=True, the kernel returns immediately rather
        than waiting for the next vblank ack. Used during shader
        transitions (#214): a primary-plane PageFlip is in flight on
        the same CRTC and the kernel would otherwise serialize our
        overlay commit behind it (~16-30ms wait per tick). Different
        plane = no data race, just a scheduling hint to the kernel
        that we don't need synchronous landing.

        For steady-state (slide-internal motion ticks), the default
        blocking commit is correct -- pacing to vblank gives clean
        visual cadence without the overhead of a queued non-block
        commit competing with itself.

        After commit, the staging buffer clears and the changes take
        effect at the next vblank regardless of which mode."""
        if not self._pending_props:
            return
        # Build (plane_id, [(prop_id, value), ...]) groups from the
        # flat staging dict.
        by_plane: dict[int, list[tuple[int, int]]] = {}
        for (plane_id, prop_id), value in self._pending_props.items():
            by_plane.setdefault(plane_id, []).append((prop_id, value))
        object_props = list(by_plane.items())
        flags = DRM_MODE_ATOMIC_NONBLOCK if nonblock else 0
        self._atomic_commit(flags=flags, object_props=object_props)
        self._pending_props.clear()

    # ---- internal staging helpers ----

    def _require_animated_slot(self, slot_idx: int) -> _PlaneSlot:
        if not 0 <= slot_idx < len(self._animated_planes):
            raise IndexError(
                f"animated slot {slot_idx} out of range "
                f"[0..{len(self._animated_planes) - 1}]"
            )
        return self._animated_planes[slot_idx]

    def _write_plane_buffer_subregion(
        self, slot: _PlaneSlot, rgba_bytes: bytes, src_w: int, src_h: int
    ) -> None:
        """Write src_w*src_h*4 RGBA bytes into the top-left subregion
        of the plane's mmap'd buffer. The remainder of the buffer is
        zeroed (so a previous attach's tail bytes don't leak). BGRA
        channel swizzle (LE arm64 ARGB8888 byte order = B,G,R,A) via
        Pillow split/merge. RGB is pre-multiplied by alpha because
        vc4 plane composition runs in PREMULTI mode by default and
        the smoke (2026-05-02) confirmed that PREMULTI is the only
        mode where vc4 honors per-pixel alpha correctly under a
        plane.alpha multiplier (COVERAGE on vc4 ignores per-pixel
        alpha, leaving an opaque-black bbox under partial plane.alpha).

        SRC_W/H on the plane (set by attach_animated_layer) tells HVS
        to only read this top-left subregion; vc4 LBM consumption
        scales with SRC_W, not the fb's full width."""
        if slot.mmap is None:
            raise RuntimeError(f"plane {slot.plane_id} mmap is None")
        expected = src_w * src_h * 4
        if len(rgba_bytes) != expected:
            raise ValueError(
                f"plane {slot.plane_id}: rgba_bytes length {len(rgba_bytes)} "
                f"!= {src_w}x{src_h} ARGB8888 ({expected})"
            )
        image = Image.frombytes("RGBA", (src_w, src_h), rgba_bytes)
        r, g, b, a = image.split()
        # Pre-multiply RGB by alpha (ImageChops.multiply = floor(x*y/255)).
        r_pm = ImageChops.multiply(r, a)
        g_pm = ImageChops.multiply(g, a)
        b_pm = ImageChops.multiply(b, a)
        payload = Image.merge("RGBA", (b_pm, g_pm, r_pm, a)).tobytes()
        # Zero whole buffer first so old subregion data doesn't leak
        # into the next attach (e.g. previous slide's bigger glyph
        # bbox bleeding around the current smaller one).
        slot.mmap[: slot.dumb_size] = b"\x00" * slot.dumb_size
        # Write the subregion's rows at the buffer's row pitch.
        src_row_bytes = src_w * 4
        for y in range(src_h):
            dst_offset = y * slot.dumb_pitch
            src_offset = y * src_row_bytes
            slot.mmap[dst_offset : dst_offset + src_row_bytes] = (
                payload[src_offset : src_offset + src_row_bytes]
            )

    def _stage_plane_detach(self, slot: _PlaneSlot) -> None:
        """Stage CRTC_ID=0, FB_ID=0 to disable the plane. Other
        properties are irrelevant when detached but harmless to leave
        at last-known values."""
        self._stage_plane_props(slot, fb_id=0, crtc_id=0)

    def _stage_plane_props(
        self,
        slot: _PlaneSlot,
        *,
        fb_id: int | None = None,
        crtc_id: int | None = None,
        crtc_x: int | None = None,
        crtc_y: int | None = None,
        crtc_w: int | None = None,
        crtc_h: int | None = None,
        src_x: int | None = None,
        src_y: int | None = None,
        src_w: int | None = None,
        src_h: int | None = None,
        alpha: int | None = None,
        zpos: int | None = None,
        pixel_blend_mode: int | None = None,
    ) -> None:
        """Generic property-staging helper. Each kwarg maps to a DRM
        plane property id (looked up in slot.props at allocation
        time). None = leave unchanged."""
        mapping = (
            ("FB_ID", fb_id),
            ("CRTC_ID", crtc_id),
            ("CRTC_X", crtc_x),
            ("CRTC_Y", crtc_y),
            ("CRTC_W", crtc_w),
            ("CRTC_H", crtc_h),
            ("SRC_X", src_x),
            ("SRC_Y", src_y),
            ("SRC_W", src_w),
            ("SRC_H", src_h),
            ("alpha", alpha),
            ("zpos", zpos),
            ("pixel blend mode", pixel_blend_mode),
        )
        for name, value in mapping:
            if value is None:
                continue
            prop_id = slot.props.get(name)
            if prop_id is None:
                # Optional props (alpha, zpos) — log once if requested
                # but missing; required props would have failed at
                # allocation.
                log.debug(
                    "DRM: plane %d has no property %r; skipping stage",
                    slot.plane_id, name,
                )
                continue
            self._pending_props[(slot.plane_id, prop_id)] = int(value)

    # --- legacy single-overlay composite path ---

    def render_composite(
        self,
        primary_rgb: bytes | None = None,
        overlay_rgba: bytes | None = None,
    ) -> None:
        """Update the primary and/or overlay plane in one call.

        primary_rgb: width*height*3 bytes of RGB888 at sign-side dims.
            Format-converted (RGB565 or XRGB8888) and written to the
            sign-native primary mmap. None leaves the primary plane
            untouched.
        overlay_rgba: width*height*4 bytes of RGBA8888 at sign-side dims.
            Channel-swizzled to ARGB8888 (B,G,R,A in memory on LE
            arm64) and written to the sign-native overlay mmap. None
            leaves the overlay plane untouched. Requires
            enable_overlay=True.

        Both planes' fbs are at sign-native dims; the vc4 HVS scales
        each to its CRTC dest rect (the letterboxed display region) at
        scanout. No atomic commit per call — the planes were bound at
        open and keep reading their FB_IDs. The GPU composites overlay-
        over-primary at scanout — no software alpha-blend.
        """
        if self._mmap is None:
            raise RuntimeError("DRMRenderer not opened")
        if overlay_rgba is not None and not self.enable_overlay:
            raise RuntimeError(
                "render_composite() got overlay_rgba but enable_overlay=False"
            )
        if primary_rgb is not None:
            self.render_frame(primary_rgb)
        if overlay_rgba is not None:
            self._write_overlay(overlay_rgba)

    def _write_overlay(self, rgba: bytes) -> None:
        """Swizzle RGBA → ARGB8888 (B,G,R,A on LE arm64) and write into
        the sign-native overlay mmap. No software scaling, no paste —
        the HVS scales the plane to its CRTC dest rect at scanout.

        At 128×96 the per-channel split/merge is sub-millisecond and
        the mmap write is 49 KB; the whole call clears in a couple ms.
        """
        assert self._overlay_mmap is not None
        expected = self.width * self.height * 4
        if len(rgba) != expected:
            raise ValueError(
                f"overlay length {len(rgba)} != {self.width}x{self.height} "
                f"RGBA8888 ({expected})"
            )
        image = Image.frombytes("RGBA", (self.width, self.height), rgba)
        r, g, b, a = image.split()
        payload = Image.merge("RGBA", (b, g, r, a)).tobytes()

        row_bytes = self.width * 4
        if self._overlay_dumb_pitch == row_bytes:
            self._overlay_mmap[: len(payload)] = payload
        else:
            for y in range(self.height):
                start = y * row_bytes
                self._overlay_mmap[
                    y * self._overlay_dumb_pitch
                    : y * self._overlay_dumb_pitch + row_bytes
                ] = payload[start : start + row_bytes]
