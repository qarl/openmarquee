#!/usr/bin/env python3
"""Phase 7 GLES2 spike #2 — render a textured-quad on the dev Pi via
EGL→GBM→GLES2, then scan it out on the HDMI display via dmabuf →
drmModeAddFB2 → drmModeSetCrtc.

Validates the full Python-side GL→display pipeline before we commit to
the production ShaderCompositor module. Uses GBM_BO_USE_LINEAR so the
spike avoids T_TILED modifier handling — production will use T_TILED
with drmModeAddFB2WithModifiers for the bandwidth win.

Run on the Pi as `openmarquee` user. Requires DRM master, so the
welcome loop must be stopped first. Holds the gradient on screen for
5 seconds, then tears down (display goes black after that — restart
the welcome loop or any other DRM-master client to recover).

  sudo pkill -f phase6_welcome_loop.py
  sudo PYTHONPATH=backend python3 scripts/phase7_shader_spike.py
"""

from __future__ import annotations

import ctypes
import os
import sys
import time

# PyOpenGL's per-context tracker needs to know we're on EGL, not GLX,
# or any state-tracking GL call (glVertexAttribPointer, etc.) raises
# "Attempt to retrieve context when no valid context". Must be set
# BEFORE the OpenGL import.
os.environ.setdefault("PYOPENGL_PLATFORM", "egl")

from OpenGL import EGL as egl  # noqa: E402
from OpenGL import GLES2 as gl  # noqa: E402

# ---------------------------------------------------------------------------
# Constants Mesa knows about but PyOpenGL's headers don't ship.
# ---------------------------------------------------------------------------
EGL_PLATFORM_GBM_KHR = 0x31D7

# fourcc('A','R','2','4') == DRM_FORMAT_ARGB8888 == GBM_FORMAT_ARGB8888.
GBM_FORMAT_ARGB8888 = (
    ord("A") | (ord("R") << 8) | (ord("2") << 16) | (ord("4") << 24)
)
DRM_FORMAT_ARGB8888 = GBM_FORMAT_ARGB8888

# from gbm.h
GBM_BO_USE_SCANOUT = 1 << 0
GBM_BO_USE_RENDERING = 1 << 2
GBM_BO_USE_LINEAR = 1 << 4

DRM_MODE_CONNECTED = 1


# ---------------------------------------------------------------------------
# Library bindings.
# ---------------------------------------------------------------------------
_gbm = ctypes.CDLL("libgbm.so.1")
_libegl = ctypes.CDLL("libEGL.so.1")
# use_errno=True so ctypes.get_errno() reflects libdrm's errno after a
# failed ioctl (defaults to 0 without the flag).
_libdrm = ctypes.CDLL("libdrm.so.2", use_errno=True)

# GBM surface management.
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


# gbm_bo_get_handle returns a `union gbm_bo_handle` by VALUE. The union
# is { uint32_t u32; int32_t s32; uint64_t u64; void *ptr; } — 8 bytes
# either way. We only ever read .u32. Returning by-value out of a
# ctypes binding is brittle, so wrap as a Structure of u64 and pull the
# low 32 bits.
class _GbmBoHandle(ctypes.Structure):
    _fields_ = [("u64", ctypes.c_uint64)]


_gbm.gbm_bo_get_handle.restype = _GbmBoHandle
_gbm.gbm_bo_get_handle.argtypes = [ctypes.c_void_p]

_gbm.gbm_bo_get_stride.restype = ctypes.c_uint32
_gbm.gbm_bo_get_stride.argtypes = [ctypes.c_void_p]

_gbm.gbm_bo_get_width.restype = ctypes.c_uint32
_gbm.gbm_bo_get_width.argtypes = [ctypes.c_void_p]

_gbm.gbm_bo_get_height.restype = ctypes.c_uint32
_gbm.gbm_bo_get_height.argtypes = [ctypes.c_void_p]

_gbm.gbm_bo_get_format.restype = ctypes.c_uint32
_gbm.gbm_bo_get_format.argtypes = [ctypes.c_void_p]

_gbm.gbm_bo_get_modifier.restype = ctypes.c_uint64
_gbm.gbm_bo_get_modifier.argtypes = [ctypes.c_void_p]


# EGL: bind native-pointer-taking entries directly because PyOpenGL's
# wrappers byref() those args and barf on the GBM device/surface ints.
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


# DRM mode resources, plane resources — we use libdrm's high-level
# wrappers instead of raw ioctls because spike code doesn't need the
# minimal-deps virtue that drm_kms.py was built for.

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

# drmModeAddFB2(fd, w, h, fmt, handles[4], pitches[4], offsets[4], &fb_id, flags)
_libdrm.drmModeAddFB2.restype = ctypes.c_int
_libdrm.drmModeAddFB2.argtypes = [
    ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32),  # handles[4]
    ctypes.POINTER(ctypes.c_uint32),  # pitches[4]
    ctypes.POINTER(ctypes.c_uint32),  # offsets[4]
    ctypes.POINTER(ctypes.c_uint32),  # &fb_id (output)
    ctypes.c_uint32,                  # flags
]

_libdrm.drmModeRmFB.restype = ctypes.c_int
_libdrm.drmModeRmFB.argtypes = [ctypes.c_int, ctypes.c_uint32]

# drmModeSetCrtc(fd, crtc_id, fb_id, x, y, &connector_id, count, &mode)
_libdrm.drmModeSetCrtc.restype = ctypes.c_int
_libdrm.drmModeSetCrtc.argtypes = [
    ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32,
    ctypes.c_uint32, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_int,
    ctypes.POINTER(_DrmModeModeInfo),
]


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _open_drm_card() -> int:
    return os.open("/dev/dri/card0", os.O_RDWR | os.O_CLOEXEC)


def _check_egl(label: str) -> None:
    err = egl.eglGetError()
    if err != egl.EGL_SUCCESS:
        raise RuntimeError(f"{label}: EGL error 0x{err:04x}")


def _check_gl(label: str) -> None:
    err = gl.glGetError()
    if err != gl.GL_NO_ERROR:
        raise RuntimeError(f"{label}: GL error 0x{err:04x}")


def _gl_str(name: int) -> str:
    return ctypes.cast(gl.glGetString(name), ctypes.c_char_p).value.decode()


def _compile_shader(kind: int, source: str) -> int:
    sh = gl.glCreateShader(kind)
    gl.glShaderSource(sh, source)
    gl.glCompileShader(sh)
    if not gl.glGetShaderiv(sh, gl.GL_COMPILE_STATUS):
        log = gl.glGetShaderInfoLog(sh).decode()
        raise RuntimeError(f"shader compile failed:\n{log}\n--source--\n{source}")
    return sh


def _link_program(vs_src: str, fs_src: str) -> int:
    vs = _compile_shader(gl.GL_VERTEX_SHADER, vs_src)
    fs = _compile_shader(gl.GL_FRAGMENT_SHADER, fs_src)
    prog = gl.glCreateProgram()
    gl.glAttachShader(prog, vs)
    gl.glAttachShader(prog, fs)
    gl.glLinkProgram(prog)
    if not gl.glGetProgramiv(prog, gl.GL_LINK_STATUS):
        raise RuntimeError(f"link failed: {gl.glGetProgramInfoLog(prog).decode()}")
    gl.glDeleteShader(vs)
    gl.glDeleteShader(fs)
    return prog


VS = """#version 100
attribute vec2 a_pos;
varying vec2 v_uv;
void main() {
  // Flip Y here so GL's bottom-up framebuffer matches the top-down
  // scanout the display expects. Cheaper than a CPU-side flip and
  // keeps glReadPixels-style debugging consistent.
  v_uv = vec2(a_pos.x * 0.5 + 0.5, 0.5 - a_pos.y * 0.5);
  gl_Position = vec4(a_pos, 0.0, 1.0);
}
"""

FS = """#version 100
precision mediump float;
varying vec2 v_uv;
void main() {
  // Big openMarquee-friendly gradient with a corner color marker so we
  // can confirm orientation on glass. (1,0)=red, (0,1)=green,
  // (0,0)=blue corner marker, (1,1)=white-ish.
  vec3 col = vec3(v_uv.x, v_uv.y, 1.0 - v_uv.x * v_uv.y);
  gl_FragColor = vec4(col, 1.0);
}
"""


def _find_active_connector_and_crtc(fd: int) -> tuple[int, int, _DrmModeModeInfo]:
    """Walk DRM resources to find one connected output + its CRTC + a
    valid mode. Returns (connector_id, crtc_id, mode_info)."""
    res_ptr = _libdrm.drmModeGetResources(fd)
    if not res_ptr:
        raise RuntimeError("drmModeGetResources returned NULL")
    try:
        res = res_ptr.contents
        for i in range(res.count_connectors):
            conn_id = res.connectors[i]
            conn_ptr = _libdrm.drmModeGetConnector(fd, conn_id)
            if not conn_ptr:
                continue
            try:
                conn = conn_ptr.contents
                if conn.connection != DRM_MODE_CONNECTED or conn.count_modes == 0:
                    continue
                mode = _DrmModeModeInfo.from_buffer_copy(
                    bytes(ctypes.string_at(
                        ctypes.addressof(conn.modes.contents),
                        ctypes.sizeof(_DrmModeModeInfo),
                    ))
                )
                # Encoder picks our CRTC. Prefer the connector's current
                # encoder if one is bound; else use the first encoder on
                # its list.
                enc_id = conn.encoder_id or conn.encoders[0]
                enc_ptr = _libdrm.drmModeGetEncoder(fd, enc_id)
                if not enc_ptr:
                    continue
                try:
                    enc = enc_ptr.contents
                    crtc_id = enc.crtc_id or res.crtcs[0]
                finally:
                    _libdrm.drmModeFreeEncoder(enc_ptr)
                return conn_id, crtc_id, mode
            finally:
                _libdrm.drmModeFreeConnector(conn_ptr)
    finally:
        _libdrm.drmModeFreeResources(res_ptr)
    raise RuntimeError("no connected DRM connector found")


def main() -> int:
    fd = _open_drm_card()
    print(f"opened card0 fd={fd}")

    conn_id, crtc_id, mode = _find_active_connector_and_crtc(fd)
    width, height = mode.hdisplay, mode.vdisplay
    print(
        f"connector={conn_id} crtc={crtc_id} "
        f"mode={mode.name.decode()} {width}x{height}@{mode.vrefresh}Hz"
    )

    gbm_dev = _gbm.gbm_create_device(fd)
    if not gbm_dev:
        raise RuntimeError("gbm_create_device returned NULL")

    egl.eglGetError()
    display = _libegl.eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, gbm_dev, None)
    if not display:
        raise RuntimeError("eglGetPlatformDisplay returned NULL")

    major = egl.EGLint(0)
    minor = egl.EGLint(0)
    egl.eglInitialize(display, ctypes.byref(major), ctypes.byref(minor))
    _check_egl("eglInitialize")
    egl.eglBindAPI(egl.EGL_OPENGL_ES_API)

    # Match config to GBM_FORMAT_ARGB8888.
    cfg_attribs = (egl.EGLint * 13)(
        egl.EGL_RED_SIZE, 8,
        egl.EGL_GREEN_SIZE, 8,
        egl.EGL_BLUE_SIZE, 8,
        egl.EGL_ALPHA_SIZE, 8,
        egl.EGL_RENDERABLE_TYPE, egl.EGL_OPENGL_ES2_BIT,
        egl.EGL_SURFACE_TYPE, egl.EGL_WINDOW_BIT,
        egl.EGL_NONE,
    )
    n_configs = egl.EGLint(0)
    if not egl.eglGetConfigs(display, None, 0, ctypes.byref(n_configs)):
        raise RuntimeError("eglGetConfigs(count) failed")
    configs = (egl.EGLConfig * n_configs.value)()
    found = egl.EGLint(0)
    egl.eglChooseConfig(
        display, cfg_attribs, configs, n_configs.value, ctypes.byref(found)
    )
    config = None
    for i in range(found.value):
        vid = egl.EGLint(0)
        egl.eglGetConfigAttrib(
            display, configs[i], egl.EGL_NATIVE_VISUAL_ID, ctypes.byref(vid)
        )
        if vid.value == GBM_FORMAT_ARGB8888:
            config = configs[i]
            break
    if config is None:
        raise RuntimeError("no EGL config matches GBM_FORMAT_ARGB8888")

    ctx_attribs = (egl.EGLint * 3)(egl.EGL_CONTEXT_CLIENT_VERSION, 2, egl.EGL_NONE)
    context = egl.eglCreateContext(display, config, egl.EGL_NO_CONTEXT, ctx_attribs)

    # USE_LINEAR keeps the dmabuf format == DRM_FORMAT_ARGB8888 with no
    # modifier, so drmModeAddFB2 (no modifiers flag) accepts it. Costs a
    # bandwidth optimization vs T_TILED but the spike just needs pixels
    # on glass; production module switches to T_TILED with
    # drmModeAddFB2WithModifiers.
    gbm_surf = _gbm.gbm_surface_create(
        gbm_dev, width, height, GBM_FORMAT_ARGB8888,
        GBM_BO_USE_RENDERING | GBM_BO_USE_SCANOUT | GBM_BO_USE_LINEAR,
    )
    if not gbm_surf:
        raise RuntimeError("gbm_surface_create returned NULL")

    config_ptr = ctypes.cast(config, ctypes.c_void_p).value
    egl_surf = _libegl.eglCreateWindowSurface(display, config_ptr, gbm_surf, None)
    err = egl.eglGetError()
    if not egl_surf:
        raise RuntimeError(f"eglCreateWindowSurface failed (err=0x{err:04x})")

    if not egl.eglMakeCurrent(display, egl_surf, egl_surf, context):
        _check_egl("eglMakeCurrent")
        raise RuntimeError("eglMakeCurrent failed")

    print(f"GL_VENDOR={_gl_str(gl.GL_VENDOR)}")
    print(f"GL_RENDERER={_gl_str(gl.GL_RENDERER)}")
    print(f"GL_VERSION={_gl_str(gl.GL_VERSION)}")

    prog = _link_program(VS, FS)
    gl.glUseProgram(prog)
    quad = (ctypes.c_float * 8)(-1, -1, 1, -1, -1, 1, 1, 1)
    vbo = gl.glGenBuffers(1)
    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
    gl.glBufferData(gl.GL_ARRAY_BUFFER, ctypes.sizeof(quad), quad, gl.GL_STATIC_DRAW)
    loc = gl.glGetAttribLocation(prog, "a_pos")
    gl.glEnableVertexAttribArray(loc)
    gl.glVertexAttribPointer(loc, 2, gl.GL_FLOAT, False, 0, None)

    gl.glViewport(0, 0, width, height)
    gl.glClearColor(0.0, 0.0, 0.0, 1.0)
    gl.glClear(gl.GL_COLOR_BUFFER_BIT)
    gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)
    _check_gl("after draw")

    # SwapBuffers makes the rendered frame the surface's "front" buffer
    # and queues the next render target as the "back" buffer. lock_front
    # gives us a gbm_bo we can derive a DRM fb from.
    if not _libegl.eglSwapBuffers(display, egl_surf):
        _check_egl("eglSwapBuffers")
        raise RuntimeError("eglSwapBuffers failed")

    bo = _gbm.gbm_surface_lock_front_buffer(gbm_surf)
    if not bo:
        raise RuntimeError("gbm_surface_lock_front_buffer returned NULL")

    handle = _gbm.gbm_bo_get_handle(bo)
    stride = _gbm.gbm_bo_get_stride(bo)
    fmt = _gbm.gbm_bo_get_format(bo)
    modifier = _gbm.gbm_bo_get_modifier(bo)
    print(
        f"bo handle=0x{handle.u64 & 0xffffffff:x} stride={stride} "
        f"fmt=0x{fmt:08x} modifier=0x{modifier:016x}"
    )

    handles = (ctypes.c_uint32 * 4)(handle.u64 & 0xFFFFFFFF, 0, 0, 0)
    pitches = (ctypes.c_uint32 * 4)(stride, 0, 0, 0)
    offsets = (ctypes.c_uint32 * 4)(0, 0, 0, 0)
    fb_id = ctypes.c_uint32(0)
    rc = _libdrm.drmModeAddFB2(
        fd, width, height, DRM_FORMAT_ARGB8888,
        handles, pitches, offsets, ctypes.byref(fb_id), 0,
    )
    if rc != 0:
        raise RuntimeError(f"drmModeAddFB2 failed rc={rc} errno={ctypes.get_errno()}")
    print(f"AddFB2 -> fb_id={fb_id.value}")

    conn_arr = (ctypes.c_uint32 * 1)(conn_id)
    rc = _libdrm.drmModeSetCrtc(
        fd, crtc_id, fb_id.value, 0, 0,
        conn_arr, 1, ctypes.byref(mode),
    )
    if rc != 0:
        raise RuntimeError(f"drmModeSetCrtc failed rc={rc}")
    print("SetCrtc OK — gradient should be on screen now")

    print("holding 5 seconds...")
    time.sleep(5)

    # Tear down: blank the CRTC so the display doesn't keep displaying a
    # buffer we're about to RmFB. Pass fb_id=0 and mode=NULL.
    null_mode = ctypes.POINTER(_DrmModeModeInfo)()
    _libdrm.drmModeSetCrtc(fd, crtc_id, 0, 0, 0, conn_arr, 1, null_mode)
    _libdrm.drmModeRmFB(fd, fb_id.value)
    _gbm.gbm_surface_release_buffer(gbm_surf, bo)

    egl.eglMakeCurrent(
        display, egl.EGL_NO_SURFACE, egl.EGL_NO_SURFACE, egl.EGL_NO_CONTEXT
    )
    egl.eglDestroySurface(display, egl_surf)
    egl.eglDestroyContext(display, context)
    _gbm.gbm_surface_destroy(ctypes.c_void_p(gbm_surf))
    egl.eglTerminate(display)
    _gbm.gbm_device_destroy(ctypes.c_void_p(gbm_dev))
    os.close(fd)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
