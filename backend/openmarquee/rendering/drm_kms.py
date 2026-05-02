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
  Phase 2a-2 adds an opt-in overlay plane in ARGB8888, held at
  sign-native dims and scaled to the letterboxed CRTC region by
  the vc4 HVS. With the overlay buffer at 128×96 ARGB instead of
  display-native, per-frame overlay updates are sub-millisecond
  (no software upscale, no software alpha-blend) — the actual
  compositor win this whole rewrite was for.

This is a Linux-only module; tests on the Mac side mock the
ioctls. The Pi-side live-fire is the canonical correctness check.
"""

from __future__ import annotations

import ctypes
import fcntl
import logging
import mmap
import os
import struct
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)


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
# mode-set commit; per-frame commits leave it off.
DRM_MODE_ATOMIC_ALLOW_MODESET = 0x0400


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
    ):
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive")
        if pixel_format not in ("rgb565", "xrgb8888"):
            raise ValueError(f"unsupported pixel_format {pixel_format!r}")
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
        """Replace the legacy SETCRTC mode-set with an atomic commit.

        Same single-primary-plane behavior as before — display lights up
        with our framebuffer at the chosen mode — but the path is now
        property-driven, which is the prerequisite for adding an
        overlay plane in the next commit.
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
        # Fixed-point 16.16 SRC dims (DRM convention).
        src_w = self.display_width << 16
        src_h = self.display_height << 16
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
                (pp["SRC_W"], src_w),
                (pp["SRC_H"], src_h),
                (pp["CRTC_X"], 0),
                (pp["CRTC_Y"], 0),
                (pp["CRTC_W"], self.display_width),
                (pp["CRTC_H"], self.display_height),
            ]),
        ]
        if self.enable_overlay:
            op = self._overlay_plane_props
            # Overlay SRC = full sign-native fb (HVS reads everything).
            # Overlay CRTC = letterboxed region of the display so the
            # GPU-scaled overlay aligns pixel-for-pixel with the
            # primary plane's letterboxed sign content.
            sign_src_w = self.width << 16
            sign_src_h = self.height << 16
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
            "DRM: atomic mode-set committed (%s)",
            "primary + overlay" if self.enable_overlay else "primary only",
        )

    # --- framebuffer alloc + map ---

    def _allocate_framebuffer(self) -> None:
        """Create a dumb buffer at display dims, mmap it, register it as
        a DRM framebuffer with XRGB8888 format. The mmap'd region is
        what render_frame writes to each tick — one userspace→display
        copy, no syscall per pixel."""
        assert self._fd is not None and self._mode is not None
        create = _DrmModeCreateDumb()
        create.width = self.display_width
        create.height = self.display_height
        create.bpp = self._bytes_per_pixel * 8
        _ioctl(self._fd, DRM_IOCTL_MODE_CREATE_DUMB, create)
        self._dumb_handle = create.handle
        self._dumb_size = int(create.size)
        self._dumb_pitch = int(create.pitch)

        # Add the dumb buffer as a DRM framebuffer.
        fb = _DrmModeFbCmd2()
        fb.width = self.display_width
        fb.height = self.display_height
        fb.pixel_format = self._drm_format
        fb.handles[0] = self._dumb_handle
        fb.pitches[0] = self._dumb_pitch
        _ioctl(self._fd, DRM_IOCTL_MODE_ADDFB2, fb)
        self._fb_id = fb.fb_id

        # mmap the dumb buffer at the offset MAP_DUMB returns.
        map_dumb = _DrmModeMapDumb()
        map_dumb.handle = self._dumb_handle
        _ioctl(self._fd, DRM_IOCTL_MODE_MAP_DUMB, map_dumb)
        self._mmap = mmap.mmap(
            self._fd,
            self._dumb_size,
            mmap.MAP_SHARED,
            mmap.PROT_READ | mmap.PROT_WRITE,
            offset=int(map_dumb.offset),
        )

    # --- close ---

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

    def render_frame(self, frame: bytes) -> None:
        """Convert RGB888 `frame` to XRGB8888 and write to the mmap'd buffer.

        `frame` is `width * height * 3` bytes (sign-side dims). We resize
        to display dims with NEAREST + letterbox (same shape HDMIRenderer
        used), then convert RGB → XRGB8888 by inserting a 0xFF padding
        byte after each pixel triplet (memory layout: B G R X per pixel
        on little-endian arm64 — DRM_FORMAT_XRGB8888 has X in the high
        byte of the 32-bit word, which is the LAST byte in memory).
        """
        if self._mmap is None:
            raise RuntimeError("DRMRenderer not opened")
        expected = self.width * self.height * 3
        if len(frame) != expected:
            raise ValueError(
                f"frame length {len(frame)} != {self.width}x{self.height} RGB888 ({expected})"
            )

        image = Image.frombytes("RGB", (self.width, self.height), frame)
        if (self.width, self.height) != (self.display_width, self.display_height):
            image = self._scale_with_letterbox(image)

        if self.pixel_format == "rgb565":
            # Pillow's "BGR;16" mode produces RGB565 little-endian — same
            # path HDMIRenderer's fb0 RGB565 uses. ~30 ms at 1080p (C-side
            # convert) and only 4 MB/frame to copy. Deprecated in Pillow
            # 12; numpy fallback for forward-compat with that release.
            import warnings
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    payload = image.convert("BGR;16").tobytes()
            except (ValueError, OSError):
                arr = np.frombuffer(image.tobytes(), dtype=np.uint8).reshape(
                    image.height, image.width, 3
                ).astype(np.uint16, copy=False)
                packed = (
                    ((arr[..., 0] & 0xF8) << 8)
                    | ((arr[..., 1] & 0xFC) << 3)
                    | (arr[..., 2] >> 3)
                )
                payload = packed.astype("<u2").tobytes()
        else:
            # XRGB8888 — bytes [B, G, R, X] per pixel. Split/merge runs
            # at ~80 ms at 1080p (C-side per-channel rearrange). Use this
            # only when we need the alpha channel (multi-plane phase).
            r, g, b = image.split()
            alpha = Image.new("L", image.size, 255)
            payload = Image.merge("RGBA", (b, g, r, alpha)).tobytes()

        # mmap slice-assignment is a single C-level memcpy from the
        # bytes object's buffer into the kernel-mapped page. If the
        # dumb buffer's pitch is wider than width*bpp (some drivers
        # pad rows), fall back to per-row copy.
        row_bytes = self.display_width * self._bytes_per_pixel
        if self._dumb_pitch == row_bytes:
            self._mmap[: len(payload)] = payload
        else:
            for y in range(self.display_height):
                start = y * row_bytes
                self._mmap[
                    y * self._dumb_pitch : y * self._dumb_pitch + row_bytes
                ] = payload[start : start + row_bytes]

    def _scale_with_letterbox(self, image: Image.Image) -> Image.Image:
        scale = min(
            self.display_width / self.width,
            self.display_height / self.height,
        )
        new_w = max(1, int(round(self.width * scale)))
        new_h = max(1, int(round(self.height * scale)))
        scaled = image.resize((new_w, new_h), resample=Image.Resampling.NEAREST)
        canvas = Image.new("RGB", (self.display_width, self.display_height), (0, 0, 0))
        off_x = (self.display_width - new_w) // 2
        off_y = (self.display_height - new_h) // 2
        canvas.paste(scaled, (off_x, off_y))
        return canvas

    # --- multi-plane composite path ---

    def render_composite(
        self,
        primary_rgb: bytes | None = None,
        overlay_rgba: bytes | None = None,
    ) -> None:
        """Update the primary and/or overlay plane in one call.

        primary_rgb: width*height*3 bytes of RGB888 (sign-side dims).
            Goes through the same scaling + format-pack path as
            render_frame. None leaves the primary plane untouched.
        overlay_rgba: width*height*4 bytes of RGBA8888 (sign-side dims).
            Scaled + letterboxed (transparent letterbox) to display dims,
            channel-swizzled to ARGB8888 (B,G,R,A in memory on LE arm64),
            written to the overlay plane's buffer. None leaves the
            overlay plane untouched. Requires enable_overlay=True.

        No atomic commit per call: the planes were bound at open and
        keep reading their FB_IDs; in-place mmap writes show up at the
        next scanout. The GPU composites overlay-over-primary at scanout
        — no software alpha-blend, which is the entire point of moving
        off the single-plane fb0/legacy path on Pi Zero 2 W (CPU blend
        at 1080p costs 208 ms, beyond the 30 fps budget).
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
