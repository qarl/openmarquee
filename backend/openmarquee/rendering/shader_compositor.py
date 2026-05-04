"""GLES2 shader compositor — slide-to-slide transitions only.

Hybrid architecture (qarl 2026-05-03 evening):

  - Multi-plane DRM (gpu_compositor.py + drm_kms.py) keeps doing the
    within-slide layer compositing. The vc4 HVS does the alpha blend
    + scaling at scanout, with zero per-pixel CPU work, well within
    the perf budget at 1080p. No change to that path.

  - This module runs ONLY during slide-to-slide transitions: a
    2-input fragment shader takes "from" and "to" slide snapshots
    and a `transition_t: 0..1` uniform, mixes them per-pixel, and
    drives the primary plane FB during the transition window.

  - Same module also covers Photoshop-style blend modes via a
    similar 2-input + blend_mode_id pattern (multiply, screen,
    overlay, soft-light, hard-light, color-dodge, color-burn,
    lighten, darken, difference) — vc4's HVS only implements
    plane.alpha+PREMULTI; everything else needs the shader.

Per-pixel cost: 2 texture2D + 1 mix + 1 write ≈ 8-12 ALU ops per
pixel × 2M pixels × 30 fps = ~600 MOps/sec. WELL within vc4 V3D 2.1's
~16 GFLOPS budget. The earlier Milestone B 8-slot blend ladder was
GPU-bound (8.6 fps at 1080p); the 2-input transition path isn't.

Bindings + DRM/EGL/GBM/GL plumbing are unchanged from the Milestone A/B
scaffolding (see commit a4347d2 / 9b2ea0c). The hot rewrite is the
fragment shader + the layer-slot API → 2-input + transition_t.

This is a Linux/vc4-specific module; tests on the Mac side mock the
renderer. The Pi-side live-fire (scripts/phase7_shader_renderer_smoke.py)
is the canonical correctness check.
"""

from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path

# PyOpenGL's per-context tracker needs to know we're on EGL, not GLX,
# or any state-tracking GL call (glVertexAttribPointer, etc.) raises
# "Attempt to retrieve context when no valid context". MUST be set
# BEFORE the OpenGL import. See project_python_gles_gotchas.md.
os.environ.setdefault("PYOPENGL_PLATFORM", "egl")

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants Mesa / drm_fourcc.h know but PyOpenGL / Python's stdlib don't.
# ---------------------------------------------------------------------------
EGL_PLATFORM_GBM_KHR = 0x31D7

# fourcc('A','R','2','4') == DRM_FORMAT_ARGB8888 == GBM_FORMAT_ARGB8888.
DRM_FORMAT_ARGB8888 = (
    ord("A") | (ord("R") << 8) | (ord("2") << 16) | (ord("4") << 24)
)
GBM_FORMAT_ARGB8888 = DRM_FORMAT_ARGB8888

# from gbm.h
GBM_BO_USE_SCANOUT = 1 << 0
GBM_BO_USE_RENDERING = 1 << 2
GBM_BO_USE_LINEAR = 1 << 4

DRM_MODE_CONNECTED = 1
DRM_MODE_PAGE_FLIP_EVENT = 0x01


# ---------------------------------------------------------------------------
# Library bindings. Loaded lazily so this module imports cleanly on the
# Mac dev side (where libdrm/libgbm/libEGL aren't present), allowing
# unit tests to mock the compositor without touching real libraries.
# ---------------------------------------------------------------------------
_libs_loaded = False
_gbm: ctypes.CDLL | None = None
_libegl: ctypes.CDLL | None = None
_libdrm: ctypes.CDLL | None = None


class _GbmBoHandle(ctypes.Structure):
    """gbm_bo_handle is a union {u32, s32, u64, void*}; ctypes
    return-by-value of unions is brittle, so we read it as a single
    u64 and pull the low 32 bits. See project_python_gles_gotchas.md."""
    _fields_ = [("u64", ctypes.c_uint64)]


class _DrmModeRes(ctypes.Structure):
    _fields_ = [
        ("count_fbs", ctypes.c_int),
        ("fbs", ctypes.POINTER(ctypes.c_uint32)),
        ("count_crtcs", ctypes.c_int),
        ("crtcs", ctypes.POINTER(ctypes.c_uint32)),
        ("count_connectors", ctypes.c_int),
        ("connectors", ctypes.POINTER(ctypes.c_uint32)),
        ("count_encoders", ctypes.c_int),
        ("encoders", ctypes.POINTER(ctypes.c_uint32)),
        ("min_width", ctypes.c_uint32),
        ("max_width", ctypes.c_uint32),
        ("min_height", ctypes.c_uint32),
        ("max_height", ctypes.c_uint32),
    ]


class _DrmModeModeInfo(ctypes.Structure):
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


class _DrmModeConnector(ctypes.Structure):
    _fields_ = [
        ("connector_id", ctypes.c_uint32),
        ("encoder_id", ctypes.c_uint32),
        ("connector_type", ctypes.c_uint32),
        ("connector_type_id", ctypes.c_uint32),
        ("connection", ctypes.c_uint32),
        ("mmWidth", ctypes.c_uint32),
        ("mmHeight", ctypes.c_uint32),
        ("subpixel", ctypes.c_uint32),
        ("count_modes", ctypes.c_int),
        ("modes", ctypes.POINTER(_DrmModeModeInfo)),
        ("count_props", ctypes.c_int),
        ("props", ctypes.POINTER(ctypes.c_uint32)),
        ("prop_values", ctypes.POINTER(ctypes.c_uint64)),
        ("count_encoders", ctypes.c_int),
        ("encoders", ctypes.POINTER(ctypes.c_uint32)),
    ]


class _DrmModeEncoder(ctypes.Structure):
    _fields_ = [
        ("encoder_id", ctypes.c_uint32),
        ("encoder_type", ctypes.c_uint32),
        ("crtc_id", ctypes.c_uint32),
        ("possible_crtcs", ctypes.c_uint32),
        ("possible_clones", ctypes.c_uint32),
    ]


def _load_libs() -> None:
    global _libs_loaded, _gbm, _libegl, _libdrm
    if _libs_loaded:
        return
    _gbm = ctypes.CDLL("libgbm.so.1")
    _libegl = ctypes.CDLL("libEGL.so.1")
    # use_errno=True surfaces ioctl errno via ctypes.get_errno() —
    # critical for diagnosing failed AddFB2 / SetCrtc.
    _libdrm = ctypes.CDLL("libdrm.so.2", use_errno=True)

    _gbm.gbm_create_device.restype = ctypes.c_void_p
    _gbm.gbm_create_device.argtypes = [ctypes.c_int]
    _gbm.gbm_device_destroy.restype = None
    _gbm.gbm_device_destroy.argtypes = [ctypes.c_void_p]
    _gbm.gbm_surface_create.restype = ctypes.c_void_p
    _gbm.gbm_surface_create.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.c_uint32,
    ]
    _gbm.gbm_surface_destroy.restype = None
    _gbm.gbm_surface_destroy.argtypes = [ctypes.c_void_p]
    _gbm.gbm_surface_lock_front_buffer.restype = ctypes.c_void_p
    _gbm.gbm_surface_lock_front_buffer.argtypes = [ctypes.c_void_p]
    _gbm.gbm_surface_release_buffer.restype = None
    _gbm.gbm_surface_release_buffer.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _gbm.gbm_bo_get_handle.restype = _GbmBoHandle
    _gbm.gbm_bo_get_handle.argtypes = [ctypes.c_void_p]
    _gbm.gbm_bo_get_stride.restype = ctypes.c_uint32
    _gbm.gbm_bo_get_stride.argtypes = [ctypes.c_void_p]

    # PyOpenGL's eglGetPlatformDisplay + eglCreateWindowSurface byref()
    # the native_display/native_window args; bind them direct.
    _libegl.eglGetPlatformDisplay.restype = ctypes.c_void_p
    _libegl.eglGetPlatformDisplay.argtypes = [
        ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
    ]
    _libegl.eglCreateWindowSurface.restype = ctypes.c_void_p
    _libegl.eglCreateWindowSurface.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    _libegl.eglSwapBuffers.restype = ctypes.c_uint32
    _libegl.eglSwapBuffers.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    _libdrm.drmModeGetResources.restype = ctypes.POINTER(_DrmModeRes)
    _libdrm.drmModeGetResources.argtypes = [ctypes.c_int]
    _libdrm.drmModeFreeResources.restype = None
    _libdrm.drmModeFreeResources.argtypes = [ctypes.POINTER(_DrmModeRes)]
    _libdrm.drmModeGetConnector.restype = ctypes.POINTER(_DrmModeConnector)
    _libdrm.drmModeGetConnector.argtypes = [ctypes.c_int, ctypes.c_uint32]
    _libdrm.drmModeFreeConnector.restype = None
    _libdrm.drmModeFreeConnector.argtypes = [ctypes.POINTER(_DrmModeConnector)]
    _libdrm.drmModeGetEncoder.restype = ctypes.POINTER(_DrmModeEncoder)
    _libdrm.drmModeGetEncoder.argtypes = [ctypes.c_int, ctypes.c_uint32]
    _libdrm.drmModeFreeEncoder.restype = None
    _libdrm.drmModeFreeEncoder.argtypes = [ctypes.POINTER(_DrmModeEncoder)]
    _libdrm.drmModeAddFB2.restype = ctypes.c_int
    _libdrm.drmModeAddFB2.argtypes = [
        ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_uint32,
    ]
    _libdrm.drmModeRmFB.restype = ctypes.c_int
    _libdrm.drmModeRmFB.argtypes = [ctypes.c_int, ctypes.c_uint32]
    _libdrm.drmModeSetCrtc.restype = ctypes.c_int
    _libdrm.drmModeSetCrtc.argtypes = [
        ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_uint32, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_int,
        ctypes.POINTER(_DrmModeModeInfo),
    ]
    _libdrm.drmModePageFlip.restype = ctypes.c_int
    _libdrm.drmModePageFlip.argtypes = [
        ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
        ctypes.c_void_p,
    ]

    _libs_loaded = True


# ---------------------------------------------------------------------------
# Shaders.
# ---------------------------------------------------------------------------

# Vertex shader: pass-through fullscreen quad with Y-flipped UV so the
# scanned-out frame matches top-down display orientation.
_VERTEX_SHADER = """#version 100
attribute vec2 a_pos;
varying vec2 v_uv;
void main() {
  v_uv = vec2(a_pos.x * 0.5 + 0.5, 0.5 - a_pos.y * 0.5);
  gl_Position = vec4(a_pos, 0.0, 1.0);
}
"""

# Transition fragment shader: 2 samplers + transition_t. Different
# transition kinds (fade, wipe, iris, dissolve, ...) each get their
# own compiled program; selection happens via _compile_transition().
# Per-pixel cost is one 2-tex sample + one mix() + the kind-specific
# mask math (a step + maybe a distance/dot/hash). Trivially fits on
# vc4 V3D 2.1 at 1080p × 30 fps.
#
# u_from = outgoing slide snapshot, u_to = incoming slide snapshot.
# u_transition_t goes 0 -> 1 over the transition duration; at 0 the
# screen shows u_from unchanged, at 1 it shows u_to unchanged.

_FRAGMENT_FADE = """#version 100
precision mediump float;
uniform sampler2D u_from;
uniform sampler2D u_to;
uniform float u_transition_t;
varying vec2 v_uv;

void main() {
  vec4 a = texture2D(u_from, v_uv);
  vec4 b = texture2D(u_to, v_uv);
  gl_FragColor = mix(a, b, u_transition_t);
}
"""

# Iris: u_to reveals through a circle that expands from screen center
# to the corners. The 0.71 max-radius covers the diagonal — center to
# corner distance in normalized [0,1] UV space is sqrt(0.5) = 0.707.
_FRAGMENT_IRIS = """#version 100
precision mediump float;
uniform sampler2D u_from;
uniform sampler2D u_to;
uniform float u_transition_t;
varying vec2 v_uv;

void main() {
  vec4 a = texture2D(u_from, v_uv);
  vec4 b = texture2D(u_to, v_uv);
  float r = distance(v_uv, vec2(0.5));
  float mask = step(r, u_transition_t * 0.71);
  gl_FragColor = mix(a, b, mask);
}
"""

# Dissolve: per-pixel reveal threshold sampled from a hash of v_uv.
# Each pixel "rolls a die" once and reveals when transition_t crosses
# its threshold — reads as a noise-driven crossfade matching the PIL
# path (np.random.default_rng().random per pixel). The hash is
# deterministic per fragment, so no per-frame jitter; smooth reveal.
_FRAGMENT_DISSOLVE = """#version 100
// highp -- the canonical sin/dot/fract hash needs the *43758.5453
// product to wrap well past the integer range before fract() peels
// off the fractional part. mediump on vc4 V3D 2.1 (~10-bit mantissa
// in fragment shaders) drops bits before fract, collapsing adjacent
// pixels to identical thresholds and producing visible diagonal
// banding instead of pepper-noise. highp gives ~24-bit and is
// emulated by Mesa on vc4 even though hardware mediump is the
// default. Per pre-commit review (#197).
precision highp float;
uniform sampler2D u_from;
uniform sampler2D u_to;
uniform float u_transition_t;
varying vec2 v_uv;

float _hash(vec2 p) {
  return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453);
}

void main() {
  vec4 a = texture2D(u_from, v_uv);
  vec4 b = texture2D(u_to, v_uv);
  float threshold = _hash(v_uv);
  float mask = step(threshold, u_transition_t);
  gl_FragColor = mix(a, b, mask);
}
"""

# Pixelate: both images sample at a coarsened grid whose block size
# grows to a peak at midpoint, then shrinks back. The peak (0.04 in UV
# = ~80 px at 1080p) gives an obvious chunky-blocks moment around
# t=0.5; ends are ~native resolution. mix() blends a → b with t so the
# whole motion reads as "current slide pixelates harder, then sharpens
# into the next."
_FRAGMENT_PIXELATE = """#version 100
precision mediump float;
uniform sampler2D u_from;
uniform sampler2D u_to;
uniform float u_transition_t;
varying vec2 v_uv;

void main() {
  // Wave: 0 at t=0/1, 1 at t=0.5. (1 - 4(t-0.5)^2).
  float wave = 1.0 - 4.0 * (u_transition_t - 0.5) * (u_transition_t - 0.5);
  // 0.0025 base = ~5px blocks at 1080p (effectively native);
  // 0.0425 peak = ~80px at midpoint.
  float blockSize = 0.0025 + 0.04 * wave;
  vec2 cell = floor(v_uv / blockSize) * blockSize + 0.5 * blockSize;
  vec4 a = texture2D(u_from, cell);
  vec4 b = texture2D(u_to, cell);
  gl_FragColor = mix(a, b, u_transition_t);
}
"""

# Scanline: top-to-bottom sweep with a bright band at the sweep line.
# Pixels above the sweep show u_to; below show u_from; the band is a
# brightness boost at the sweep edge. Matches the PIL scanline's CRT
# look (band height ~3% of panel = 0.03 in UV).
_FRAGMENT_SCANLINE = """#version 100
precision mediump float;
uniform sampler2D u_from;
uniform sampler2D u_to;
uniform float u_transition_t;
varying vec2 v_uv;

void main() {
  vec4 a = texture2D(u_from, v_uv);
  vec4 b = texture2D(u_to, v_uv);
  float sweep = u_transition_t;
  float band_half = 0.015;  // half-height of the bright band
  // Pixels above the sweep line are b; below are a.
  float mask = step(v_uv.y, sweep);
  vec4 col = mix(a, b, mask);
  // Bright glow: white-ish boost where v_uv.y is within band of sweep.
  float band = 1.0 - smoothstep(0.0, band_half, abs(v_uv.y - sweep));
  col.rgb = mix(col.rgb, vec3(1.0), band * 0.7);
  gl_FragColor = col;
}
"""

# Halftone: u_to emerges through a regular grid of growing circular
# dots, one per cell. Cell pitch = ~8 cells across the smaller dim
# (matches the PIL halftone's pitch = min(w,h) // 8). Per-cell radius
# grows 0 -> ~half-diagonal over t, so by t=1 every dot covers its
# cell entirely and u_to is fully revealed.
#
# Aspect correction: 16:9 hardcoded for the HDMI 1080p target -- LED
# matrix and fb0 paths don't reach the shader compositor (they lack
# drm_fd). On a non-16:9 panel cells would render as ovals; that's a
# visual compromise to defer until non-16:9 deployments matter.
_FRAGMENT_HALFTONE = """#version 100
precision mediump float;
uniform sampler2D u_from;
uniform sampler2D u_to;
uniform float u_transition_t;
varying vec2 v_uv;

void main() {
  vec4 a = texture2D(u_from, v_uv);
  vec4 b = texture2D(u_to, v_uv);
  // 8 cells vertical (smaller dim), 8 * 16/9 ~= 14 horizontal on 1080p.
  // fract(uv * grid) gives cell-local UV in [0,1).
  float grid_y = 8.0;
  float aspect = 16.0 / 9.0;
  vec2 cell_uv = fract(vec2(v_uv.x * grid_y * aspect, v_uv.y * grid_y));
  // Distance to cell center, in cell-relative coords.
  float d = distance(cell_uv, vec2(0.5));
  // Radius grows 0 -> 0.71 (cell half-diagonal) over t.
  float mask = step(d, u_transition_t * 0.71);
  gl_FragColor = mix(a, b, mask);
}
"""

# Glitch: digital-corruption look. Per-row horizontal jitter + linear
# cross-fade + occasional cyan tear rows. Animated frame-to-frame so
# the corruption "breaks" actively rather than sitting still -- the
# canonical "screen tearing" read.
#
# frame_seed below quantizes u_transition_t into ~30 distinct buckets
# over the transition window, so the per-row hash gets a fresh seed
# every frame even though u_transition_t is the only time uniform.
_FRAGMENT_GLITCH = """#version 100
precision highp float;
uniform sampler2D u_from;
uniform sampler2D u_to;
uniform float u_transition_t;
varying vec2 v_uv;

float _hash(vec2 p) {
  return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453);
}

void main() {
  // ~1080 rows at 1080p; floor() to get a stable per-row index.
  float row = floor(v_uv.y * 1080.0);
  // 30 distinct frame buckets across the transition window. Each
  // bucket gets its own per-row jitter -- looks like new corruption
  // appearing every frame.
  float frame_seed = floor(u_transition_t * 30.0);
  float jitter = (_hash(vec2(row, frame_seed)) - 0.5) * 0.1 * u_transition_t;
  vec2 uv2 = vec2(v_uv.x + jitter, v_uv.y);
  vec4 a = texture2D(u_from, uv2);
  vec4 b = texture2D(u_to, uv2);
  vec4 col = mix(a, b, u_transition_t);
  // Tear bands: ~5% of rows in each frame get a cyan overlay. Hash
  // (coarser row groups, frame_seed) so tears are wider than 1px and
  // jump around per frame.
  float tear_row = floor(v_uv.y * 60.0);  // 60 horizontal bands
  float tear = step(0.95, _hash(vec2(tear_row, frame_seed + 1.0)));
  col.rgb = mix(col.rgb, vec3(0.0, 1.0, 1.0), tear * 0.5 * u_transition_t);
  gl_FragColor = col;
}
"""

# Transition-kind ID -> fragment shader source. Add new kinds here as
# their per-fragment math is worked out; ShaderRenderer compiles each
# program at startup and picks one per transition via set_kind().
_TRANSITION_SHADERS: dict[str, str] = {
    "fade": _FRAGMENT_FADE,
    "iris": _FRAGMENT_IRIS,
    "dissolve": _FRAGMENT_DISSOLVE,
    "pixelate": _FRAGMENT_PIXELATE,
    "scanline": _FRAGMENT_SCANLINE,
    "halftone": _FRAGMENT_HALFTONE,
    "glitch": _FRAGMENT_GLITCH,
}


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _check_egl(label: str) -> None:
    from OpenGL import EGL as _e
    err = _e.eglGetError()
    if err != _e.EGL_SUCCESS:
        raise RuntimeError(f"{label}: EGL error 0x{err:04x}")


def _check_gl(label: str) -> None:
    from OpenGL import GLES2 as _g
    err = _g.glGetError()
    if err != _g.GL_NO_ERROR:
        raise RuntimeError(f"{label}: GL error 0x{err:04x}")


def _gl_str(name: int) -> str:
    """Read a GL_VENDOR/GL_RENDERER/GL_VERSION string. PyOpenGL returns
    a GLubyteArray of unspecified length; cast to c_char_p for the
    NUL-terminated read."""
    from OpenGL import GLES2 as _g
    return ctypes.cast(_g.glGetString(name), ctypes.c_char_p).value.decode()


def _compile_shader(kind: int, source: str) -> int:
    from OpenGL import GLES2 as _g
    sh = _g.glCreateShader(kind)
    _g.glShaderSource(sh, source)
    _g.glCompileShader(sh)
    if not _g.glGetShaderiv(sh, _g.GL_COMPILE_STATUS):
        log_ = _g.glGetShaderInfoLog(sh).decode()
        raise RuntimeError(f"shader compile failed:\n{log_}\n--source--\n{source}")
    return sh


def _link_program(vs_src: str, fs_src: str) -> int:
    from OpenGL import GLES2 as _g
    vs = _compile_shader(_g.GL_VERTEX_SHADER, vs_src)
    fs = _compile_shader(_g.GL_FRAGMENT_SHADER, fs_src)
    prog = _g.glCreateProgram()
    try:
        _g.glAttachShader(prog, vs)
        _g.glAttachShader(prog, fs)
        _g.glLinkProgram(prog)
        # Mark shaders for deletion now that they're attached. GL ref-
        # counts them: they stay alive until the program is destroyed
        # OR detached. This way a link failure can't leak the shader
        # objects even when the program itself is glDeleteProgram'd.
        _g.glDeleteShader(vs)
        _g.glDeleteShader(fs)
        if not _g.glGetProgramiv(prog, _g.GL_LINK_STATUS):
            raise RuntimeError(
                f"program link failed: {_g.glGetProgramInfoLog(prog).decode()}"
            )
        return prog
    except Exception:
        _g.glDeleteProgram(prog)
        raise


# ---------------------------------------------------------------------------
# ShaderRenderer — owns DRM master + GL context for the lifetime of a
# transition. Activated only during slide-to-slide transitions and
# Photoshop blend modes; multi-plane DRM does within-slide compositing.
# ---------------------------------------------------------------------------


class ShaderRenderer:
    """GLES2 shader-driven transition renderer. Owns DRM master +
    EGL/GBM/GL context. Renders frames through a 2-input fragment
    shader (u_from + u_to + u_transition_t); output goes directly to a
    GBM-backed buffer scanned out via DRM.

    Usage (from PlaybackLoop transition handlers or smoke script):

        with ShaderRenderer() as r:
            r.set_kind("fade")
            r.set_from(snapshot_a_rgba, w, h)
            r.set_to(snapshot_b_rgba, w, h)
            for i in range(n_frames):
                r.set_transition_t(i / (n_frames - 1))
                r.commit_frame()

    Width/height are auto-derived from the connector's preferred mode
    (1920x1080 on the dev Pi). DRM-fd ownership has two modes:

    * Default (no `drm_fd` arg): we open `/dev/dri/card0`, take DRM
      master at __enter__, blank the CRTC + close the fd at close().
      The welcome loop / multi-plane compositor must be stopped first.

    * Shared (`drm_fd` arg): the caller's renderer (typically
      DRMRenderer in multi-plane mode) holds master and passes its
      live fd in. We do plane discovery + GBM/EGL setup against the
      same fd; commit_frame's SetCrtc/PageFlip uses the existing
      master authorization. close() releases GL/EGL/GBM but does NOT
      close the fd or blank the CRTC — the caller resumes scanout by
      SetCrtc'ing back to its own primary fb. This is the path
      PlaybackLoop drives during a slide-to-slide transition window.
    """

    def __init__(
        self,
        *,
        device_path: Path = Path("/dev/dri/card0"),
        drm_fd: int | None = None,
    ) -> None:
        self.device_path = Path(device_path)
        # When drm_fd is supplied, the caller owns master + fd lifecycle.
        # close() must not close it nor blank the CRTC.
        self._owns_fd: bool = drm_fd is None
        self.width: int = 0
        self.height: int = 0
        self._fd: int = -1 if drm_fd is None else drm_fd
        self._gbm_dev: int = 0  # gbm_device*, raw int
        self._gbm_surf: int = 0  # gbm_surface*, raw int
        self._egl_display: int = 0
        self._egl_context: int = 0
        self._egl_surface: int = 0
        # GL state — one program per transition kind, two textures
        # (from + to slide snapshots), shared VBO/quad. _programs maps
        # kind name -> compiled program id; _kind_locs caches the
        # uniform locations per kind so set_kind() is O(1).
        self._programs: dict[str, int] = {}
        self._kind_locs: dict[str, dict[str, int]] = {}
        self._active_kind: str = "fade"
        self._vbo: int = 0
        self._a_pos_loc: int = -1
        # Two textures, fixed: unit 0 = from, unit 1 = to. The shader
        # pipeline never grows beyond two inputs in the hybrid model;
        # multi-layer compositing stays on the multi-plane DRM path.
        self._tex_from: int = 0
        self._tex_to: int = 0
        self._transition_t: float = 0.0
        # Connector / CRTC discovered at open time; mode held in
        # self._mode (kept alive — drmModeSetCrtc reads it by ref).
        self._connector_id: int = 0
        self._crtc_id: int = 0
        self._mode: _DrmModeModeInfo | None = None
        self._connector_arr: ctypes.Array | None = None
        # FB swap-chain state. Each commit:
        #   1. SwapBuffers (queues current-back to front)
        #   2. lock_front_buffer (gives us the bo just rendered)
        #   3. AddFB2 wrapping that bo
        #   4. SetCrtc (first frame) or PageFlip (subsequent)
        #   5. After the *next* commit displaces this bo, release it.
        # _pending_bo is the bo that was scanned out last commit; we
        # release it on the NEXT commit, after the new bo is bound.
        self._pending_bo: int = 0
        self._pending_fb_id: int = 0
        self._setcrtc_done: bool = False

    def __enter__(self) -> "ShaderRenderer":
        _load_libs()
        try:
            self._open_drm()
            self._init_egl_gbm()
            self._compile_program()
            self._upload_initial_bg()
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # --- lifecycle steps ---

    def _open_drm(self) -> None:
        assert _libdrm is not None
        if self._owns_fd:
            self._fd = os.open(
                str(self.device_path), os.O_RDWR | os.O_CLOEXEC,
            )
            log.info("DRM(shader): opened %s fd=%d", self.device_path, self._fd)
        else:
            log.info(
                "DRM(shader): using shared fd=%d (caller owns master)",
                self._fd,
            )

        res_ptr = _libdrm.drmModeGetResources(self._fd)
        if not res_ptr:
            raise RuntimeError("drmModeGetResources returned NULL")
        try:
            res = res_ptr.contents
            for i in range(res.count_connectors):
                conn_id = res.connectors[i]
                conn_ptr = _libdrm.drmModeGetConnector(self._fd, conn_id)
                if not conn_ptr:
                    continue
                try:
                    conn = conn_ptr.contents
                    if conn.connection != DRM_MODE_CONNECTED or conn.count_modes == 0:
                        continue
                    # Copy the mode by value — the connector struct gets
                    # freed at the end of this loop, but the mode must
                    # outlive it for the SetCrtc call.
                    self._mode = _DrmModeModeInfo()
                    ctypes.memmove(
                        ctypes.addressof(self._mode),
                        ctypes.addressof(conn.modes[0]),
                        ctypes.sizeof(_DrmModeModeInfo),
                    )
                    enc_id = conn.encoder_id or conn.encoders[0]
                    enc_ptr = _libdrm.drmModeGetEncoder(self._fd, enc_id)
                    if not enc_ptr:
                        continue
                    try:
                        enc = enc_ptr.contents
                        crtc_id = enc.crtc_id or res.crtcs[0]
                    finally:
                        _libdrm.drmModeFreeEncoder(enc_ptr)
                    self._connector_id = conn_id
                    self._crtc_id = crtc_id
                    self.width = self._mode.hdisplay
                    self.height = self._mode.vdisplay
                    self._connector_arr = (ctypes.c_uint32 * 1)(conn_id)
                    log.info(
                        "DRM(shader): connector=%d crtc=%d mode=%s "
                        "%dx%d@%dHz",
                        conn_id, crtc_id, self._mode.name.decode(),
                        self.width, self.height, self._mode.vrefresh,
                    )
                    return
                finally:
                    _libdrm.drmModeFreeConnector(conn_ptr)
        finally:
            _libdrm.drmModeFreeResources(res_ptr)
        raise RuntimeError("no connected DRM connector found")

    def _init_egl_gbm(self) -> None:
        from OpenGL import EGL as _e
        assert _gbm is not None and _libegl is not None

        self._gbm_dev = _gbm.gbm_create_device(self._fd) or 0
        if not self._gbm_dev:
            raise RuntimeError("gbm_create_device returned NULL")

        # Drain any sticky EGL error before our first call, so the
        # post-call error reflects only what happened in this call.
        _e.eglGetError()
        self._egl_display = (
            _libegl.eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, self._gbm_dev, None)
            or 0
        )
        if not self._egl_display:
            raise RuntimeError("eglGetPlatformDisplay(GBM) returned NULL")

        major = _e.EGLint(0)
        minor = _e.EGLint(0)
        _e.eglInitialize(self._egl_display, ctypes.byref(major), ctypes.byref(minor))
        _check_egl("eglInitialize")
        log.info("EGL(shader): %d.%d", major.value, minor.value)
        _e.eglBindAPI(_e.EGL_OPENGL_ES_API)
        _check_egl("eglBindAPI")

        cfg_attribs = (_e.EGLint * 13)(
            _e.EGL_RED_SIZE, 8,
            _e.EGL_GREEN_SIZE, 8,
            _e.EGL_BLUE_SIZE, 8,
            _e.EGL_ALPHA_SIZE, 8,
            _e.EGL_RENDERABLE_TYPE, _e.EGL_OPENGL_ES2_BIT,
            _e.EGL_SURFACE_TYPE, _e.EGL_WINDOW_BIT,
            _e.EGL_NONE,
        )
        n_configs = _e.EGLint(0)
        if not _e.eglGetConfigs(
            self._egl_display, None, 0, ctypes.byref(n_configs)
        ):
            raise RuntimeError("eglGetConfigs(count) failed")
        configs = (_e.EGLConfig * n_configs.value)()
        found = _e.EGLint(0)
        _e.eglChooseConfig(
            self._egl_display, cfg_attribs, configs, n_configs.value,
            ctypes.byref(found),
        )
        # kmscube's reference pattern: pick the config whose
        # EGL_NATIVE_VISUAL_ID matches our GBM format. Grabbing the
        # first config can give one with the wrong native pixel layout
        # and hand garbled scanout.
        config = None
        for i in range(found.value):
            vid = _e.EGLint(0)
            _e.eglGetConfigAttrib(
                self._egl_display, configs[i],
                _e.EGL_NATIVE_VISUAL_ID, ctypes.byref(vid),
            )
            if vid.value == GBM_FORMAT_ARGB8888:
                config = configs[i]
                break
        if config is None:
            raise RuntimeError("no EGL config matches GBM_FORMAT_ARGB8888")

        ctx_attribs = (_e.EGLint * 3)(
            _e.EGL_CONTEXT_CLIENT_VERSION, 2, _e.EGL_NONE,
        )
        self._egl_context = _e.eglCreateContext(
            self._egl_display, config, _e.EGL_NO_CONTEXT, ctx_attribs,
        )
        _check_egl("eglCreateContext")

        # USE_LINEAR for milestone A — skips T_TILED modifier handling so
        # drmModeAddFB2 (no modifiers flag) accepts the dmabuf. Sacrifices
        # a bandwidth optimization vs T_TILED + drmModeAddFB2WithModifiers,
        # but we're well inside the budget at 1080p single-bg. Future
        # milestone will switch to T_TILED for the multi-layer case where
        # bandwidth tightens.
        self._gbm_surf = (
            _gbm.gbm_surface_create(
                self._gbm_dev, self.width, self.height, GBM_FORMAT_ARGB8888,
                GBM_BO_USE_RENDERING | GBM_BO_USE_SCANOUT | GBM_BO_USE_LINEAR,
            )
            or 0
        )
        if not self._gbm_surf:
            raise RuntimeError("gbm_surface_create returned NULL")

        config_ptr = ctypes.cast(config, ctypes.c_void_p).value
        self._egl_surface = (
            _libegl.eglCreateWindowSurface(
                self._egl_display, config_ptr, self._gbm_surf, None,
            )
            or 0
        )
        err = _e.eglGetError()
        if not self._egl_surface:
            raise RuntimeError(
                f"eglCreateWindowSurface failed (err=0x{err:04x})"
            )

        if not _e.eglMakeCurrent(
            self._egl_display, self._egl_surface, self._egl_surface,
            self._egl_context,
        ):
            _check_egl("eglMakeCurrent")
            raise RuntimeError("eglMakeCurrent failed")

        log.info(
            "GL(shader): vendor=%s renderer=%s version=%s",
            _gl_str(_g_const_GL_VENDOR()),
            _gl_str(_g_const_GL_RENDERER()),
            _gl_str(_g_const_GL_VERSION()),
        )

    def _compile_program(self) -> None:
        from OpenGL import GLES2 as _g

        # Compile every transition program once at startup. Selection
        # at transition start is then a glUseProgram + uniform sampler
        # bind — no compile cost in the hot path. Programs all share
        # the same vertex shader, so vs is compiled implicitly per
        # link via _link_program (cheap).
        for kind, fs_src in _TRANSITION_SHADERS.items():
            prog = _link_program(_VERTEX_SHADER, fs_src)
            self._programs[kind] = prog
            self._kind_locs[kind] = {
                "u_from": int(_g.glGetUniformLocation(prog, "u_from")),
                "u_to": int(_g.glGetUniformLocation(prog, "u_to")),
                "u_transition_t": int(
                    _g.glGetUniformLocation(prog, "u_transition_t")
                ),
                "a_pos": int(_g.glGetAttribLocation(prog, "a_pos")),
            }

        # Set up the shared VBO + a_pos attribute for the default kind.
        # set_kind() switches programs but the attribute layout (one
        # vec2 a_pos at the start of the buffer) is identical for
        # every program, so the bind survives the program switch.
        prog = self._programs[self._active_kind]
        _g.glUseProgram(prog)
        quad = (ctypes.c_float * 8)(-1, -1, 1, -1, -1, 1, 1, 1)
        self._vbo = int(_g.glGenBuffers(1))
        _g.glBindBuffer(_g.GL_ARRAY_BUFFER, self._vbo)
        _g.glBufferData(
            _g.GL_ARRAY_BUFFER, ctypes.sizeof(quad), quad, _g.GL_STATIC_DRAW,
        )
        self._a_pos_loc = self._kind_locs[self._active_kind]["a_pos"]
        if self._a_pos_loc < 0:
            raise RuntimeError("a_pos attribute not found in vertex shader")
        _g.glEnableVertexAttribArray(self._a_pos_loc)
        _g.glVertexAttribPointer(
            self._a_pos_loc, 2, _g.GL_FLOAT, False, 0, None,
        )

        # Two textures: unit 0 = from, unit 1 = to. Both get a 1x1
        # opaque-black placeholder so the sampler is complete before
        # the first set_from/set_to.
        empty = (ctypes.c_uint8 * 4)(0, 0, 0, 255)
        self._tex_from = int(_g.glGenTextures(1))
        self._tex_to = int(_g.glGenTextures(1))
        for unit, tex in ((0, self._tex_from), (1, self._tex_to)):
            _g.glActiveTexture(_g.GL_TEXTURE0 + unit)
            _g.glBindTexture(_g.GL_TEXTURE_2D, tex)
            _g.glTexImage2D(
                _g.GL_TEXTURE_2D, 0, _g.GL_RGBA, 1, 1, 0,
                _g.GL_RGBA, _g.GL_UNSIGNED_BYTE, empty,
            )
            _g.glTexParameteri(
                _g.GL_TEXTURE_2D, _g.GL_TEXTURE_MIN_FILTER, _g.GL_LINEAR,
            )
            _g.glTexParameteri(
                _g.GL_TEXTURE_2D, _g.GL_TEXTURE_MAG_FILTER, _g.GL_LINEAR,
            )
            _g.glTexParameteri(
                _g.GL_TEXTURE_2D, _g.GL_TEXTURE_WRAP_S, _g.GL_CLAMP_TO_EDGE,
            )
            _g.glTexParameteri(
                _g.GL_TEXTURE_2D, _g.GL_TEXTURE_WRAP_T, _g.GL_CLAMP_TO_EDGE,
            )

        # Sampler bindings are per-program but always (u_from -> 0,
        # u_to -> 1). Set them on every program once.
        for kind, p in self._programs.items():
            _g.glUseProgram(p)
            locs = self._kind_locs[kind]
            if locs["u_from"] >= 0:
                _g.glUniform1i(locs["u_from"], 0)
            if locs["u_to"] >= 0:
                _g.glUniform1i(locs["u_to"], 1)
        _g.glUseProgram(self._programs[self._active_kind])

        _g.glViewport(0, 0, self.width, self.height)
        _check_gl("after _compile_program")

    def _upload_initial_bg(self) -> None:
        """Opaque-black starting frame — keeps the scanout buffer
        well-defined from the moment we take DRM master, so the viewer
        never sees whatever the prior owner left in the framebuffer.
        At t=0 with both textures = black, mix() outputs black."""
        black = b"\x00\x00\x00\xff" * (self.width * self.height)
        self.set_from(black, self.width, self.height)
        self.set_to(black, self.width, self.height)
        self._transition_t = 0.0

    # --- public API: transition inputs ---

    def set_from(self, rgba_bytes: bytes, src_w: int, src_h: int) -> None:
        """Upload the OUTGOING slide snapshot to texture unit 0. Called
        once at transition start by the caller (typically the
        multi-plane DRM compositor freezing the current display state).
        src_w * src_h * 4 bytes; the shader samples it at v_uv across
        the full display."""
        self._upload_texture(self._tex_from, 0, rgba_bytes, src_w, src_h, "from")

    def set_to(self, rgba_bytes: bytes, src_w: int, src_h: int) -> None:
        """Upload the INCOMING slide snapshot to texture unit 1. Called
        once at transition start by the caller (the new slide composited
        into a single RGBA via PIL alpha_composite at slide entry)."""
        self._upload_texture(self._tex_to, 1, rgba_bytes, src_w, src_h, "to")

    def _upload_texture(
        self, tex: int, unit: int, rgba: bytes, w: int, h: int, label: str,
    ) -> None:
        from OpenGL import GLES2 as _g
        if w <= 0 or h <= 0:
            raise ValueError(f"set_{label}: src dims must be positive ({w}x{h})")
        expected = w * h * 4
        if len(rgba) != expected:
            raise ValueError(
                f"set_{label}: rgba size mismatch — got {len(rgba)} bytes, "
                f"expected {expected} for {w}x{h} RGBA"
            )
        _g.glActiveTexture(_g.GL_TEXTURE0 + unit)
        _g.glBindTexture(_g.GL_TEXTURE_2D, tex)
        _g.glTexImage2D(
            _g.GL_TEXTURE_2D, 0, _g.GL_RGBA, w, h, 0,
            _g.GL_RGBA, _g.GL_UNSIGNED_BYTE, rgba,
        )
        _check_gl(f"set_{label} glTexImage2D")

    def set_transition_t(self, t: float) -> None:
        """Set the transition progress, 0.0..1.0. At 0 the screen
        shows u_from unchanged; at 1 it shows u_to unchanged. Caller
        drives this from the transition timeline."""
        if t < 0.0:
            t = 0.0
        elif t > 1.0:
            t = 1.0
        self._transition_t = float(t)

    def set_kind(self, kind: str) -> None:
        """Pick which transition fragment shader runs. Must match a
        key in _TRANSITION_SHADERS. Cheap (glUseProgram); no compile
        cost since every program is built at startup."""
        from OpenGL import GLES2 as _g
        if kind not in self._programs:
            raise ValueError(
                f"unknown transition kind {kind!r}; available: "
                f"{sorted(self._programs)}"
            )
        if kind == self._active_kind:
            return
        self._active_kind = kind
        _g.glUseProgram(self._programs[kind])
        # The a_pos attrib bind is identical for every program (same
        # vertex shader), but the attribute LOCATION may differ across
        # link units, so re-bind.
        self._a_pos_loc = self._kind_locs[kind]["a_pos"]
        _g.glEnableVertexAttribArray(self._a_pos_loc)
        _g.glBindBuffer(_g.GL_ARRAY_BUFFER, self._vbo)
        _g.glVertexAttribPointer(
            self._a_pos_loc, 2, _g.GL_FLOAT, False, 0, None,
        )

    def commit_frame(self) -> None:
        """Render one frame (sample bg texture across the fullscreen
        quad), present via SwapBuffers, AddFB2 the new front bo, and
        either SetCrtc (first commit) or PageFlip (subsequent)."""
        from OpenGL import EGL as _e
        from OpenGL import GLES2 as _g
        assert _gbm is not None and _libegl is not None and _libdrm is not None

        # Push the single transition_t uniform for the active program.
        # u_from / u_to sampler bindings are sticky from _compile_program;
        # only the t value changes per frame.
        loc = self._kind_locs[self._active_kind]["u_transition_t"]
        if loc >= 0:
            _g.glUniform1f(loc, self._transition_t)

        _g.glClearColor(0.0, 0.0, 0.0, 1.0)
        _g.glClear(_g.GL_COLOR_BUFFER_BIT)
        _g.glDrawArrays(_g.GL_TRIANGLE_STRIP, 0, 4)
        _check_gl("commit_frame draw")

        if not _libegl.eglSwapBuffers(self._egl_display, self._egl_surface):
            _check_egl("eglSwapBuffers")
            raise RuntimeError("eglSwapBuffers failed")

        bo = _gbm.gbm_surface_lock_front_buffer(self._gbm_surf)
        if not bo:
            raise RuntimeError("gbm_surface_lock_front_buffer returned NULL")

        handle = _gbm.gbm_bo_get_handle(bo)
        stride = _gbm.gbm_bo_get_stride(bo)
        handles = (ctypes.c_uint32 * 4)(handle.u64 & 0xFFFFFFFF, 0, 0, 0)
        pitches = (ctypes.c_uint32 * 4)(stride, 0, 0, 0)
        offsets = (ctypes.c_uint32 * 4)(0, 0, 0, 0)
        fb_id = ctypes.c_uint32(0)
        rc = _libdrm.drmModeAddFB2(
            self._fd, self.width, self.height, DRM_FORMAT_ARGB8888,
            handles, pitches, offsets, ctypes.byref(fb_id), 0,
        )
        if rc != 0:
            errno = ctypes.get_errno()
            _gbm.gbm_surface_release_buffer(self._gbm_surf, bo)
            raise RuntimeError(
                f"drmModeAddFB2 failed rc={rc} errno={errno}"
            )

        if not self._setcrtc_done:
            assert self._mode is not None and self._connector_arr is not None
            rc = _libdrm.drmModeSetCrtc(
                self._fd, self._crtc_id, fb_id.value, 0, 0,
                self._connector_arr, 1, ctypes.byref(self._mode),
            )
            if rc != 0:
                _libdrm.drmModeRmFB(self._fd, fb_id.value)
                _gbm.gbm_surface_release_buffer(self._gbm_surf, bo)
                raise RuntimeError(f"drmModeSetCrtc failed rc={rc}")
            self._setcrtc_done = True
        else:
            # PageFlip is non-blocking and vsync-tied; we don't drain
            # the page-flip event in milestone A (no fence sync yet).
            # Without DRM_MODE_PAGE_FLIP_EVENT it just queues the flip.
            rc = _libdrm.drmModePageFlip(
                self._fd, self._crtc_id, fb_id.value, 0, None,
            )
            if rc != 0:
                # Most common cause: previous page flip not yet
                # consumed. Fall back to SetCrtc (synchronous).
                rc2 = _libdrm.drmModeSetCrtc(
                    self._fd, self._crtc_id, fb_id.value, 0, 0,
                    self._connector_arr, 1, ctypes.byref(self._mode),
                )
                if rc2 != 0:
                    _libdrm.drmModeRmFB(self._fd, fb_id.value)
                    _gbm.gbm_surface_release_buffer(self._gbm_surf, bo)
                    raise RuntimeError(
                        f"drmModePageFlip failed rc={rc} and SetCrtc "
                        f"fallback failed rc={rc2}"
                    )

        # The new bo is now scanned out. Release the PREVIOUS one (and
        # RmFB its fb) — the kernel was holding a reference until the
        # CRTC moved off it. Doing it in this order ensures we never
        # release a bo that's actively scanning out.
        if self._pending_bo:
            _libdrm.drmModeRmFB(self._fd, self._pending_fb_id)
            _gbm.gbm_surface_release_buffer(self._gbm_surf, self._pending_bo)
        self._pending_bo = bo
        self._pending_fb_id = fb_id.value

    # --- teardown ---

    def close(self) -> None:
        """Tear down GL context, GBM, DRM. Idempotent."""
        from OpenGL import EGL as _e
        from OpenGL import GLES2 as _g

        # Blank the CRTC FIRST so the kernel isn't scanning out a fb
        # we're about to RmFB. SKIPPED in shared-fd mode: the caller
        # owns master and is about to SetCrtc back to its own primary
        # fb, so blanking would just add a single black frame between
        # the last shader frame and the caller's resumed scanout.
        if (
            _libdrm is not None
            and self._fd >= 0
            and self._setcrtc_done
            and self._connector_arr is not None
            and self._owns_fd
        ):
            try:
                null_mode = ctypes.POINTER(_DrmModeModeInfo)()
                _libdrm.drmModeSetCrtc(
                    self._fd, self._crtc_id, 0, 0, 0,
                    self._connector_arr, 1, null_mode,
                )
            except Exception:
                log.exception("DRM(shader): SetCrtc(blank) failed")
            self._setcrtc_done = False

        if _libdrm is not None and self._fd >= 0 and self._pending_bo:
            try:
                _libdrm.drmModeRmFB(self._fd, self._pending_fb_id)
            except Exception:
                log.exception("DRM(shader): RmFB during close failed")
            try:
                if _gbm is not None and self._gbm_surf:
                    _gbm.gbm_surface_release_buffer(
                        self._gbm_surf, self._pending_bo,
                    )
            except Exception:
                log.exception("DRM(shader): release_buffer during close failed")
            self._pending_bo = 0
            self._pending_fb_id = 0

        # GL objects.
        if self._egl_display and self._egl_context:
            try:
                # PyOpenGL's glDeleteBuffers / glDeleteTextures accept a
                # ctypes array reliably; the (count, list) form is auto-
                # converted on most builds but not all. Use the array
                # form for forward-compat across Pi/Mesa builds.
                live_textures = [t for t in (self._tex_from, self._tex_to) if t]
                if live_textures:
                    arr = (ctypes.c_uint * len(live_textures))(*live_textures)
                    _g.glDeleteTextures(len(live_textures), arr)
                    self._tex_from = 0
                    self._tex_to = 0
                if self._vbo:
                    arr = (ctypes.c_uint * 1)(self._vbo)
                    _g.glDeleteBuffers(1, arr)
                    self._vbo = 0
                for kind, prog in list(self._programs.items()):
                    if prog:
                        _g.glDeleteProgram(prog)
                self._programs.clear()
                self._kind_locs.clear()
            except Exception:
                log.exception("GL(shader): GL object cleanup failed")
            try:
                _e.eglMakeCurrent(
                    self._egl_display, _e.EGL_NO_SURFACE,
                    _e.EGL_NO_SURFACE, _e.EGL_NO_CONTEXT,
                )
            except Exception:
                pass
            if self._egl_surface:
                try:
                    _e.eglDestroySurface(self._egl_display, self._egl_surface)
                except Exception:
                    pass
                self._egl_surface = 0
            try:
                _e.eglDestroyContext(self._egl_display, self._egl_context)
            except Exception:
                pass
            self._egl_context = 0

        if _gbm is not None and self._gbm_surf:
            try:
                _gbm.gbm_surface_destroy(ctypes.c_void_p(self._gbm_surf))
            except Exception:
                pass
            self._gbm_surf = 0

        if self._egl_display:
            try:
                _e.eglTerminate(self._egl_display)
            except Exception:
                pass
            self._egl_display = 0

        if _gbm is not None and self._gbm_dev:
            try:
                _gbm.gbm_device_destroy(ctypes.c_void_p(self._gbm_dev))
            except Exception:
                pass
            self._gbm_dev = 0

        if self._fd >= 0 and self._owns_fd:
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = -1


# Constant accessors — pulled out so _init_egl_gbm's log line doesn't
# import OpenGL.GLES2 inline three times.

def _g_const_GL_VENDOR() -> int:
    from OpenGL import GLES2 as _g
    return _g.GL_VENDOR


def _g_const_GL_RENDERER() -> int:
    from OpenGL import GLES2 as _g
    return _g.GL_RENDERER


def _g_const_GL_VERSION() -> int:
    from OpenGL import GLES2 as _g
    return _g.GL_VERSION
