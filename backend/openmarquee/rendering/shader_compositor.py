"""GLES2 single-pass shader compositor — alternative to gpu_compositor.

EGL → GBM → GLES2 → dmabuf → DRM-plane atomic-commit pipeline. Single
primary plane carries the composited frame; multi-layer blending,
per-frame motion, and slide transitions all happen in the fragment
shader. Replaces GPUSlideCompositor (multi-plane DRM compositing at
scanout) when the shader compositor feature flag is on; the
multi-plane path stays in tree as the fallback.

Architecture (qarl 2026-05-03 decision; see project_shader_compositor_
decision.md and project_vc4_shader_feasibility.md):

  - ONE fragment-shader pass per frame, sampling bg + N layer textures
    in the same fragment. NEVER multi-pass FBO ping-pong — the vc4
    bandwidth budget on Pi Zero 2 W (1.2-1.6 GB/s DDR) does not allow
    a second full-resolution read+write per frame.
  - Pre-rasterize text + bg to GLES2 textures at slide entry; per-frame
    only updates uniforms (transform mat3, opacity, blend_mode int,
    transition_t).
  - Single GBM surface with double/triple-buffer rotation; locked
    front bo → drmModeAddFB2 → atomic commit. Page-flip event per
    frame ties commit to vsync.

Milestone A (this file's current state): single background texture,
single draw, single SetCrtc + page-flip per frame. Validates the
production rendering pipeline before adding layers (B), motion (C),
transitions (D), blend modes (E), fence sync (F).

This is a Linux/vc4-specific module; tests on the Mac side mock the
renderer. The Pi-side live-fire (`scripts/phase7_shader_renderer_smoke.py`)
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

# Milestone B fragment shader: 8-slot layer blend ladder, single pass.
# Slot 0 conventionally carries the background (full-screen rect);
# slots 1-7 carry text/element layers stacked on top in slot-index order.
# Each slot has a sampler2D, a vec4 dst rect (x_min, y_min, x_max, y_max
# in display UV [0,1]), and a float opacity. Inactive slots use opacity
# = 0 + degenerate rect — a one-instruction early-return inside
# _layer_contrib makes the cost negligible.
#
# GLES 2.0 doesn't allow dynamic indexing of sampler2D arrays, so the
# 8-slot ladder is unrolled. Built from N_LAYERS so changing the slot
# count is a single edit.
N_LAYERS = 8


def _build_fragment_shader(n_layers: int) -> str:
    samplers = "\n".join(f"uniform sampler2D u_layer{i};" for i in range(n_layers))
    ladder_lines = []
    for i in range(n_layers):
        ladder_lines.append(
            f"  c = _layer_contrib(u_layer{i}, u_layer_rect[{i}], "
            f"u_layer_opacity[{i}], v_uv);"
        )
        # Alpha-over for unpremultiplied input:
        #   col.rgb = mix(col.rgb, c.rgb, c.a)
        #   col.a   = c.a + col.a * (1.0 - c.a)
        # Slot 0 conventionally has rect=[0,0,1,1] + opacity=1, so its
        # mix() collapses to col.rgb = c.rgb (the bg replaces whatever
        # the clear left).
        ladder_lines.append(
            "  col.rgb = mix(col.rgb, c.rgb, c.a);"
            "  col.a = c.a + col.a * (1.0 - c.a);"
        )
    return f"""#version 100
precision mediump float;
{samplers}
uniform vec4 u_layer_rect[{n_layers}];
uniform float u_layer_opacity[{n_layers}];
varying vec2 v_uv;

vec4 _layer_contrib(sampler2D tex, vec4 rect, float opacity, vec2 uv) {{
  vec2 sz = rect.zw - rect.xy;
  if (sz.x <= 0.0 || sz.y <= 0.0 || opacity <= 0.0) return vec4(0.0);
  if (uv.x < rect.x || uv.x > rect.z ||
      uv.y < rect.y || uv.y > rect.w) return vec4(0.0);
  vec2 local = (uv - rect.xy) / sz;
  vec4 c = texture2D(tex, local);
  c.a *= opacity;
  return c;
}}

void main() {{
  // Start fully transparent. Slot 0's mix() lays the bg down on the
  // first iteration; remaining slots alpha-over on top.
  vec4 col = vec4(0.0, 0.0, 0.0, 0.0);
  vec4 c;
{chr(10).join(ladder_lines)}
  gl_FragColor = col;
}}
"""


_FRAGMENT_SHADER = _build_fragment_shader(N_LAYERS)


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
    _g.glAttachShader(prog, vs)
    _g.glAttachShader(prog, fs)
    _g.glLinkProgram(prog)
    if not _g.glGetProgramiv(prog, _g.GL_LINK_STATUS):
        raise RuntimeError(
            f"program link failed: {_g.glGetProgramInfoLog(prog).decode()}"
        )
    _g.glDeleteShader(vs)
    _g.glDeleteShader(fs)
    return prog


# ---------------------------------------------------------------------------
# ShaderRenderer — owns DRM master + GL context for the lifetime of a
# playback session. ShaderSlideCompositor (Milestone B) sits on top.
# ---------------------------------------------------------------------------


class ShaderRenderer:
    """GLES2 shader-driven HDMI renderer. Owns DRM master + EGL/GBM/GL
    context. Renders frames through a fullscreen quad fragment shader;
    output goes directly to a GBM-backed buffer scanned out via DRM.

    Usage (from PlaybackLoop or smoke script):

        with ShaderRenderer() as r:
            r.set_background(rgba_bytes_1920x1080)
            r.commit_frame()  # draws + page-flips
            ...

    Width/height are auto-derived from the connector's preferred mode
    (1920x1080 on the dev Pi). The renderer takes DRM master at __enter__
    and releases it at close — only one DRM master per device, so the
    welcome loop / multi-plane compositor must be stopped first.

    Milestone A: single background texture, no layers, no motion. Holds
    the most-recent set_background() output until next set_background().
    Milestone B will add per-layer textures + uniforms.
    """

    def __init__(
        self,
        *,
        device_path: Path = Path("/dev/dri/card0"),
    ) -> None:
        self.device_path = Path(device_path)
        self.width: int = 0
        self.height: int = 0
        self._fd: int = -1
        self._gbm_dev: int = 0  # gbm_device*, raw int
        self._gbm_surf: int = 0  # gbm_surface*, raw int
        self._egl_display: int = 0
        self._egl_context: int = 0
        self._egl_surface: int = 0
        self._program: int = 0
        self._vbo: int = 0
        self._a_pos_loc: int = -1
        # Per-layer GL state. Index = slot 0..N_LAYERS-1.
        # _tex_layer holds the texture id; 0 = unused. _u_layer_loc is
        # the sampler uniform location. _layer_active mirrors the
        # opacity > 0 + non-degenerate-rect bit so detach can flip a
        # slot inactive in O(1) without reading uniforms back.
        self._tex_layer: list[int] = [0] * N_LAYERS
        self._u_layer_loc: list[int] = [-1] * N_LAYERS
        self._u_layer_rect_loc: int = -1
        self._u_layer_opacity_loc: int = -1
        # Per-slot dst rect (x_min, y_min, x_max, y_max) in display UV
        # [0,1] and opacity [0,1]. Pushed as uniforms each commit_frame.
        # Inactive slot = rect (0,0,0,0) + opacity 0 (degenerate, shader
        # short-circuits).
        self._layer_rect: list[tuple[float, float, float, float]] = [
            (0.0, 0.0, 0.0, 0.0)
        ] * N_LAYERS
        self._layer_opacity: list[float] = [0.0] * N_LAYERS
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
        self._fd = os.open(str(self.device_path), os.O_RDWR | os.O_CLOEXEC)
        log.info("DRM(shader): opened %s fd=%d", self.device_path, self._fd)

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

        # Sanity-check the device's sampler-image-unit budget. GLES 2.0
        # spec minimum is 8; vc4 V3D 2.1 reports 16 in practice. Either
        # way we need N_LAYERS image units in the fragment shader.
        max_units = _g.glGetIntegerv(_g.GL_MAX_TEXTURE_IMAGE_UNITS)
        if int(max_units) < N_LAYERS:
            raise RuntimeError(
                f"GL_MAX_TEXTURE_IMAGE_UNITS={int(max_units)} < N_LAYERS="
                f"{N_LAYERS}; reduce N_LAYERS or split into multi-pass "
                f"(but see project_vc4_shader_feasibility.md — multi-pass "
                f"blows the bandwidth budget at 1080p)"
            )

        self._program = _link_program(_VERTEX_SHADER, _FRAGMENT_SHADER)
        _g.glUseProgram(self._program)
        # VBO holds the fullscreen-quad triangle strip. Set up once.
        quad = (ctypes.c_float * 8)(-1, -1, 1, -1, -1, 1, 1, 1)
        self._vbo = int(_g.glGenBuffers(1))
        _g.glBindBuffer(_g.GL_ARRAY_BUFFER, self._vbo)
        _g.glBufferData(
            _g.GL_ARRAY_BUFFER, ctypes.sizeof(quad), quad, _g.GL_STATIC_DRAW,
        )
        self._a_pos_loc = _g.glGetAttribLocation(self._program, "a_pos")
        if self._a_pos_loc < 0:
            raise RuntimeError("a_pos attribute not found in vertex shader")
        _g.glEnableVertexAttribArray(self._a_pos_loc)
        _g.glVertexAttribPointer(
            self._a_pos_loc, 2, _g.GL_FLOAT, False, 0, None,
        )

        # Allocate one texture per slot; uploads happen via attach_layer.
        # Bind each sampler uniform to its texture unit (slot i → unit i).
        # Texture parameters get set per-slot when we glTexImage2D the
        # actual data — defaults at allocate time would otherwise need
        # to be reset on first upload anyway.
        for i in range(N_LAYERS):
            tex_id = int(_g.glGenTextures(1))
            self._tex_layer[i] = tex_id
            _g.glActiveTexture(_g.GL_TEXTURE0 + i)
            _g.glBindTexture(_g.GL_TEXTURE_2D, tex_id)
            # 1x1 transparent placeholder so the sampler doesn't get
            # incomplete-texture warnings if a slot is sampled before
            # its first attach. (Per the shader's rect/opacity short
            # circuit, this should never actually happen, but the GLES
            # validator can flag it.)
            empty = (ctypes.c_uint8 * 4)(0, 0, 0, 0)
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
            loc = _g.glGetUniformLocation(self._program, f"u_layer{i}")
            self._u_layer_loc[i] = int(loc)
            if loc >= 0:
                _g.glUniform1i(loc, i)

        self._u_layer_rect_loc = int(
            _g.glGetUniformLocation(self._program, "u_layer_rect")
        )
        self._u_layer_opacity_loc = int(
            _g.glGetUniformLocation(self._program, "u_layer_opacity")
        )

        _g.glViewport(0, 0, self.width, self.height)
        _check_gl("after _compile_program")

    def _upload_initial_bg(self) -> None:
        """Black starting bg — keeps the scanout buffer well-defined
        from the moment we take DRM master, so the viewer never sees
        whatever the prior owner left in the framebuffer."""
        self.set_background(
            b"\x00\x00\x00\xff" * (self.width * self.height)
        )

    # --- public API: layer slots ---

    def set_background(self, rgba_bytes: bytes) -> None:
        """Convenience wrapper for the common case: upload a full-display
        RGBA image into slot 0 with a full-screen rect + opacity 1.0.
        Equivalent to attach_layer(slot=0, ...) with the rect implied."""
        expected = self.width * self.height * 4
        if len(rgba_bytes) != expected:
            raise ValueError(
                f"bg size mismatch: got {len(rgba_bytes)} bytes, expected "
                f"{expected} for {self.width}x{self.height} RGBA"
            )
        self.attach_layer(
            0, rgba_bytes, self.width, self.height,
            dst_x=0.0, dst_y=0.0, dst_w=1.0, dst_h=1.0, opacity=1.0,
        )

    def attach_layer(
        self,
        slot: int,
        rgba_bytes: bytes,
        src_w: int,
        src_h: int,
        *,
        dst_x: float,
        dst_y: float,
        dst_w: float,
        dst_h: float,
        opacity: float = 1.0,
    ) -> None:
        """Upload an RGBA bitmap into one layer slot and place it on the
        display.

        rgba_bytes is the layer's bitmap — typically the glyph-bbox
        crop from render_layer_to_rgba(). src_w/src_h are its pixel
        dims. dst_(x,y,w,h) place the layer in DISPLAY UV [0,1] space
        — top-left corner at (dst_x, dst_y), size (dst_w, dst_h).
        opacity blends the layer over whatever is below.

        Slot 0 conventionally holds the bg (full-screen rect, opacity
        1.0); slots 1..N_LAYERS-1 hold text/element layers stacked in
        slot-index order. Higher slot = drawn on top.
        """
        from OpenGL import GLES2 as _g
        if not 0 <= slot < N_LAYERS:
            raise ValueError(f"slot {slot} out of range [0, {N_LAYERS})")
        if src_w <= 0 or src_h <= 0:
            raise ValueError(f"src dims must be positive (got {src_w}x{src_h})")
        expected = src_w * src_h * 4
        if len(rgba_bytes) != expected:
            raise ValueError(
                f"layer slot {slot}: rgba size mismatch — got "
                f"{len(rgba_bytes)} bytes, expected {expected} for "
                f"{src_w}x{src_h} RGBA"
            )

        _g.glActiveTexture(_g.GL_TEXTURE0 + slot)
        _g.glBindTexture(_g.GL_TEXTURE_2D, self._tex_layer[slot])
        _g.glTexImage2D(
            _g.GL_TEXTURE_2D, 0, _g.GL_RGBA, src_w, src_h, 0,
            _g.GL_RGBA, _g.GL_UNSIGNED_BYTE, rgba_bytes,
        )
        _check_gl(f"attach_layer slot={slot} glTexImage2D")
        self._layer_rect[slot] = (
            dst_x, dst_y, dst_x + dst_w, dst_y + dst_h,
        )
        self._layer_opacity[slot] = float(opacity)

    def update_layer(
        self,
        slot: int,
        *,
        dst_x: float | None = None,
        dst_y: float | None = None,
        dst_w: float | None = None,
        dst_h: float | None = None,
        opacity: float | None = None,
    ) -> None:
        """Patch a slot's dst rect / opacity without re-uploading the
        bitmap. Hot-path helper for per-frame motion uniform updates
        in Milestone D."""
        if not 0 <= slot < N_LAYERS:
            raise ValueError(f"slot {slot} out of range [0, {N_LAYERS})")
        x_min, y_min, x_max, y_max = self._layer_rect[slot]
        cur_w = x_max - x_min
        cur_h = y_max - y_min
        new_x = x_min if dst_x is None else dst_x
        new_y = y_min if dst_y is None else dst_y
        new_w = cur_w if dst_w is None else dst_w
        new_h = cur_h if dst_h is None else dst_h
        self._layer_rect[slot] = (new_x, new_y, new_x + new_w, new_y + new_h)
        if opacity is not None:
            self._layer_opacity[slot] = float(opacity)

    def detach_layer(self, slot: int) -> None:
        """Mark a slot inactive by zeroing its opacity. Shader short-
        circuits on opacity <= 0. Rect is PRESERVED so a subsequent
        update_layer can flip the slot back on without having to re-
        supply the dst dims (Milestone D motion paths need this)."""
        if not 0 <= slot < N_LAYERS:
            raise ValueError(f"slot {slot} out of range [0, {N_LAYERS})")
        self._layer_opacity[slot] = 0.0

    def commit_frame(self) -> None:
        """Render one frame (sample bg texture across the fullscreen
        quad), present via SwapBuffers, AddFB2 the new front bo, and
        either SetCrtc (first commit) or PageFlip (subsequent)."""
        from OpenGL import EGL as _e
        from OpenGL import GLES2 as _g
        assert _gbm is not None and _libegl is not None and _libdrm is not None

        # Push per-slot uniforms. The GLES2 spec lets us set N values
        # at once via the *v variants — one ioctl-equivalent call each
        # for rect and opacity.
        rect_arr = (ctypes.c_float * (N_LAYERS * 4))()
        op_arr = (ctypes.c_float * N_LAYERS)()
        for i, (rect, op) in enumerate(zip(self._layer_rect, self._layer_opacity)):
            rect_arr[i * 4 + 0] = rect[0]
            rect_arr[i * 4 + 1] = rect[1]
            rect_arr[i * 4 + 2] = rect[2]
            rect_arr[i * 4 + 3] = rect[3]
            op_arr[i] = op
        if self._u_layer_rect_loc >= 0:
            _g.glUniform4fv(self._u_layer_rect_loc, N_LAYERS, rect_arr)
        if self._u_layer_opacity_loc >= 0:
            _g.glUniform1fv(self._u_layer_opacity_loc, N_LAYERS, op_arr)

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
        # we're about to RmFB.
        if (
            _libdrm is not None
            and self._fd >= 0
            and self._setcrtc_done
            and self._connector_arr is not None
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
                live_textures = [t for t in self._tex_layer if t]
                if live_textures:
                    arr = (ctypes.c_uint * len(live_textures))(*live_textures)
                    _g.glDeleteTextures(len(live_textures), arr)
                    self._tex_layer = [0] * N_LAYERS
                if self._vbo:
                    arr = (ctypes.c_uint * 1)(self._vbo)
                    _g.glDeleteBuffers(1, arr)
                    self._vbo = 0
                if self._program:
                    _g.glDeleteProgram(self._program)
                    self._program = 0
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

        if self._fd >= 0:
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
