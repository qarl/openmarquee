#!/usr/bin/env python3
"""Phase 7 GLES2 spike #1 — render a textured-quad on the dev Pi via
EGL→GBM→GLES2, glReadPixels back, save PNG.

Validates that we can drive the GL stack from Python on the dev Pi
before committing to the production shader compositor. The next spike
(#2) adds dmabuf export + atomic-commit on the primary plane so the
output lands on glass instead of in /tmp.

Run on the Pi as `openmarquee` user. No DRM master needed — render
node (renderD128) is fine for compute/render-only work, so the welcome
loop can keep running while this runs.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

# PyOpenGL's per-context tracker needs to know we're on EGL, not GLX,
# or any state-tracking GL call (glVertexAttribPointer, etc.) raises
# "Attempt to retrieve context when no valid context". Must be set
# BEFORE the OpenGL import.
os.environ.setdefault("PYOPENGL_PLATFORM", "egl")

from OpenGL import EGL as egl  # noqa: E402
from OpenGL import GLES2 as gl  # noqa: E402
from PIL import Image  # noqa: E402

# ---------------------------------------------------------------------------
# Constants Mesa knows about but PyOpenGL's headers don't ship.
# ---------------------------------------------------------------------------
EGL_PLATFORM_GBM_KHR = 0x31D7

# fourcc('A','R','2','4') == DRM_FORMAT_ARGB8888 == GBM_FORMAT_ARGB8888.
GBM_FORMAT_ARGB8888 = (
    ord("A") | (ord("R") << 8) | (ord("2") << 16) | (ord("4") << 24)
)

# from gbm.h — only RENDERING needed for the readback spike. SCANOUT
# comes in spike #2 when we want the BO to be eligible for a DRM fb.
GBM_BO_USE_SCANOUT = 1 << 0
GBM_BO_USE_RENDERING = 1 << 2

# ---------------------------------------------------------------------------
# Minimal libgbm ctypes binding. Just the entry points we need.
# ---------------------------------------------------------------------------
_gbm = ctypes.CDLL("libgbm.so.1")
_libegl = ctypes.CDLL("libEGL.so.1")

# PyOpenGL's eglGetPlatformDisplay wrapper byref()s native_display, which
# expects a ctypes instance and barfs on the int we get from
# gbm_create_device. Bind the C entry point directly so we can pass the
# raw pointer through. Same dance for eglGetPlatformDisplayEXT.
_libegl.eglGetPlatformDisplay.restype = ctypes.c_void_p
_libegl.eglGetPlatformDisplay.argtypes = [
    ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
]
# eglCreateWindowSurface's native_window is gbm_surface*; same byref()
# trap as eglGetPlatformDisplay, so bind it directly too.
_libegl.eglCreateWindowSurface.restype = ctypes.c_void_p
_libegl.eglCreateWindowSurface.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
]

_gbm.gbm_create_device.restype = ctypes.c_void_p
_gbm.gbm_create_device.argtypes = [ctypes.c_int]

_gbm.gbm_device_destroy.restype = None
_gbm.gbm_device_destroy.argtypes = [ctypes.c_void_p]

_gbm.gbm_surface_create.restype = ctypes.c_void_p
_gbm.gbm_surface_create.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_uint32,
]

_gbm.gbm_surface_destroy.restype = None
_gbm.gbm_surface_destroy.argtypes = [ctypes.c_void_p]


def _open_drm_card() -> int:
    """Open the vc4 KMS card node. Mesa's GBM platform display path
    expects a KMS-capable device, so renderD128 (render-only) returns
    NO_DISPLAY. We can open card0 without DRM master since we're not
    setting modes here — but DRM master holders block exclusive opens
    on some kernels, so the welcome loop must be stopped first."""
    return os.open("/dev/dri/card0", os.O_RDWR | os.O_CLOEXEC)


def _check_egl(label: str) -> None:
    err = egl.eglGetError()
    if err != egl.EGL_SUCCESS:
        raise RuntimeError(f"{label}: EGL error 0x{err:04x}")


def _check_gl(label: str) -> None:
    err = gl.glGetError()
    if err != gl.GL_NO_ERROR:
        raise RuntimeError(f"{label}: GL error 0x{err:04x}")


def _compile_shader(kind: int, source: str) -> int:
    sh = gl.glCreateShader(kind)
    gl.glShaderSource(sh, source)
    gl.glCompileShader(sh)
    status = gl.glGetShaderiv(sh, gl.GL_COMPILE_STATUS)
    if not status:
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
        log = gl.glGetProgramInfoLog(prog).decode()
        raise RuntimeError(f"link failed:\n{log}")
    gl.glDeleteShader(vs)
    gl.glDeleteShader(fs)
    return prog


VS = """#version 100
attribute vec2 a_pos;
varying vec2 v_uv;
void main() {
  v_uv = a_pos * 0.5 + 0.5;
  gl_Position = vec4(a_pos, 0.0, 1.0);
}
"""

FS = """#version 100
precision mediump float;
varying vec2 v_uv;
void main() {
  // gradient + 16x16 checker so we can verify orientation + colors
  float c = mod(floor(v_uv.x * 16.0) + floor(v_uv.y * 16.0), 2.0);
  vec3 base = vec3(v_uv.x, v_uv.y, 0.5);
  vec3 chk = mix(base, 1.0 - base, c * 0.5);
  gl_FragColor = vec4(chk, 1.0);
}
"""


def main() -> int:
    width, height = 1920, 1080
    out_path = Path("/tmp/phase7_spike.png")

    fd = _open_drm_card()
    print(f"opened card0 fd={fd}")

    gbm_dev = _gbm.gbm_create_device(fd)
    if not gbm_dev:
        raise RuntimeError("gbm_create_device returned NULL")
    print(f"gbm_device={gbm_dev:#x}")

    egl.eglGetError()
    display = _libegl.eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, gbm_dev, None)
    err = egl.eglGetError()
    print(f"eglGetPlatformDisplay -> {display!r} (err=0x{err:04x})")
    if not display:
        raise RuntimeError("no EGL display from GBM")

    major = egl.EGLint(0)
    minor = egl.EGLint(0)
    egl.eglInitialize(display, ctypes.byref(major), ctypes.byref(minor))
    _check_egl("eglInitialize")
    print(f"EGL {major.value}.{minor.value}")

    egl.eglBindAPI(egl.EGL_OPENGL_ES_API)
    _check_egl("eglBindAPI")

    # Match the config to GBM_FORMAT_ARGB8888 via EGL_NATIVE_VISUAL_ID,
    # not by grabbing the first config — kmscube's reference pattern.
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
    if not egl.eglChooseConfig(
        display, cfg_attribs, configs, n_configs.value, ctypes.byref(found)
    ):
        _check_egl("eglChooseConfig")
        raise RuntimeError("eglChooseConfig returned false")
    print(f"egl configs found={found.value}")

    config = None
    for i in range(found.value):
        vid = egl.EGLint(0)
        egl.eglGetConfigAttrib(
            display, configs[i], egl.EGL_NATIVE_VISUAL_ID, ctypes.byref(vid)
        )
        if vid.value == GBM_FORMAT_ARGB8888:
            config = configs[i]
            print(f"matched config #{i} visual_id=0x{vid.value:08x}")
            break
    if config is None:
        raise RuntimeError("no EGL config matches GBM_FORMAT_ARGB8888")

    ctx_attribs = (egl.EGLint * 3)(egl.EGL_CONTEXT_CLIENT_VERSION, 2, egl.EGL_NONE)
    context = egl.eglCreateContext(display, config, egl.EGL_NO_CONTEXT, ctx_attribs)
    _check_egl("eglCreateContext")
    if not context or context == egl.EGL_NO_CONTEXT:
        raise RuntimeError("eglCreateContext returned NO_CONTEXT")

    gbm_surf = _gbm.gbm_surface_create(
        gbm_dev,
        width,
        height,
        GBM_FORMAT_ARGB8888,
        GBM_BO_USE_RENDERING,
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

    def _gl_str(name: int) -> str:
        # PyOpenGL's glGetString returns a GLubyteArray of unspecified
        # length; cast to c_char_p to get the NUL-terminated string.
        return ctypes.cast(gl.glGetString(name), ctypes.c_char_p).value.decode()

    print(f"GL_VENDOR={_gl_str(gl.GL_VENDOR)}")
    print(f"GL_RENDERER={_gl_str(gl.GL_RENDERER)}")
    print(f"GL_VERSION={_gl_str(gl.GL_VERSION)}")

    # --- pipeline ---
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
    gl.glFinish()
    _check_gl("after draw")

    # Read back to verify the GL pipeline actually ran something. PIL
    # treats GL output as RGBA top-up but GL is bottom-up, so flip on save.
    buf = (ctypes.c_ubyte * (width * height * 4))()
    gl.glReadPixels(0, 0, width, height, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, buf)
    _check_gl("glReadPixels")
    img = Image.frombytes("RGBA", (width, height), bytes(buf))
    img = img.transpose(Image.FLIP_TOP_BOTTOM)
    img.save(out_path)
    print(f"wrote {out_path} {img.size}")

    # --- teardown ---
    egl.eglMakeCurrent(
        display, egl.EGL_NO_SURFACE, egl.EGL_NO_SURFACE, egl.EGL_NO_CONTEXT
    )
    egl.eglDestroySurface(display, egl_surf)
    egl.eglDestroyContext(display, context)
    _gbm.gbm_surface_destroy(ctypes.c_void_p(gbm_surf))
    egl.eglTerminate(display)
    _gbm.gbm_device_destroy(ctypes.c_void_p(gbm_dev))
    os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
