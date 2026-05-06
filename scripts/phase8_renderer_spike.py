#!/usr/bin/env python3
"""Phase 8 renderer-rewrite spike.

Measures the eight things the renderer-rewrite plan reviewer flagged
as either BLOCKER or IMPORTANT-needs-verification:

  1. GL limits on vc4 V3D 2.1 (MAX_TEXTURE_IMAGE_UNITS,
     MAX_FRAGMENT_UNIFORM_VECTORS, MAX_VARYING_VECTORS).
  2. Whether dynamic indexing of sampler2D arrays compiles + works
     in vc4's GLES2 fragment shader.
  3. How many concurrent samplers can actually be linked (8? 12?
     14?).
  4. Shader compile times for representative programs.
  5. Bandwidth limit: ms/frame at 1080p with N samples/fragment for
     N in {1, 2, 4, 6, 8, 14}, confirming whether the unified
     shader's 14-sample plan is feasible.
  6. EGL_EXT_image_dma_buf_import availability + modifiers
     extension presence (for H.264 dmabuf import path).
  7. glReadPixels stall cost at 1080p RGBA (snapshot endpoint
     budget).
  8. DRM rotation property availability on the primary plane (via
     /sys/class/drm + libdrm property dump).

Output: JSON to stdout, human-readable summary to stderr.

Reuses EGL/GBM bindings from scripts/phase7_shader_spike_offscreen.py
(memory: project_python_gles_gotchas.md).

Runs alongside the live backend without needing DRM master — opens
card0 read-only, uses an offscreen FBO for rendering.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("PYOPENGL_PLATFORM", "egl")

from OpenGL import EGL as egl  # noqa: E402
from OpenGL import GLES2 as gl  # noqa: E402

EGL_PLATFORM_GBM_KHR = 0x31D7
GBM_FORMAT_ARGB8888 = ord("A") | (ord("R") << 8) | (ord("2") << 16) | (ord("4") << 24)
GBM_BO_USE_RENDERING = 1 << 2

_gbm = ctypes.CDLL("libgbm.so.1")
_libegl = ctypes.CDLL("libEGL.so.1")

_libegl.eglGetPlatformDisplay.restype = ctypes.c_void_p
_libegl.eglGetPlatformDisplay.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
_libegl.eglCreateWindowSurface.restype = ctypes.c_void_p
_libegl.eglCreateWindowSurface.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
_libegl.eglQueryString.restype = ctypes.c_char_p
_libegl.eglQueryString.argtypes = [ctypes.c_void_p, ctypes.c_int]

_gbm.gbm_create_device.restype = ctypes.c_void_p
_gbm.gbm_create_device.argtypes = [ctypes.c_int]
_gbm.gbm_device_destroy.restype = None
_gbm.gbm_device_destroy.argtypes = [ctypes.c_void_p]
_gbm.gbm_surface_create.restype = ctypes.c_void_p
_gbm.gbm_surface_create.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
_gbm.gbm_surface_destroy.restype = None
_gbm.gbm_surface_destroy.argtypes = [ctypes.c_void_p]


def _open_drm_card() -> int:
    return os.open("/dev/dri/card0", os.O_RDWR | os.O_CLOEXEC)


def _setup_egl_context():
    """Bring up EGL+GBM context with a small gbm_surface (matching the
    prior phase7 spike's pattern — pbuffer is rejected on the GBM
    platform, so we need a window surface even though we'll only
    render to FBOs)."""
    drm_fd = _open_drm_card()
    gbm_dev = _gbm.gbm_create_device(drm_fd)
    if not gbm_dev:
        raise RuntimeError("gbm_create_device failed")

    display = _libegl.eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, ctypes.c_void_p(gbm_dev), None)
    if not display:
        raise RuntimeError("eglGetPlatformDisplay failed")

    if not egl.eglInitialize(display, None, None):
        raise RuntimeError(f"eglInitialize failed: 0x{egl.eglGetError():04x}")
    if not egl.eglBindAPI(egl.EGL_OPENGL_ES_API):
        raise RuntimeError(f"eglBindAPI failed: 0x{egl.eglGetError():04x}")

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
    if not egl.eglChooseConfig(display, cfg_attribs, configs, n_configs.value, ctypes.byref(found)):
        raise RuntimeError(f"eglChooseConfig failed: 0x{egl.eglGetError():04x}")

    config = None
    for i in range(found.value):
        vid = egl.EGLint(0)
        egl.eglGetConfigAttrib(display, configs[i], egl.EGL_NATIVE_VISUAL_ID, ctypes.byref(vid))
        if vid.value == GBM_FORMAT_ARGB8888:
            config = configs[i]
            break
    if config is None:
        raise RuntimeError("no EGL config matched GBM_FORMAT_ARGB8888")

    # Tiny gbm_surface just to satisfy MakeCurrent; we render to FBOs.
    gbm_surf = _gbm.gbm_surface_create(gbm_dev, 16, 16, GBM_FORMAT_ARGB8888, GBM_BO_USE_RENDERING)
    if not gbm_surf:
        raise RuntimeError("gbm_surface_create failed")

    config_ptr = ctypes.cast(config, ctypes.c_void_p).value
    surface = _libegl.eglCreateWindowSurface(display, config_ptr, gbm_surf, None)
    if not surface:
        raise RuntimeError(f"eglCreateWindowSurface failed: 0x{egl.eglGetError():04x}")

    ctx_attribs = (egl.EGLint * 3)(egl.EGL_CONTEXT_CLIENT_VERSION, 2, egl.EGL_NONE)
    context = egl.eglCreateContext(display, config, egl.EGL_NO_CONTEXT, ctx_attribs)
    if not context:
        raise RuntimeError(f"eglCreateContext failed: 0x{egl.eglGetError():04x}")

    if not egl.eglMakeCurrent(display, surface, surface, context):
        raise RuntimeError(f"eglMakeCurrent failed: 0x{egl.eglGetError():04x}")

    return display, context, surface, drm_fd, gbm_dev, gbm_surf


def _gl_int(name: int) -> int:
    out = (ctypes.c_int * 1)()
    gl.glGetIntegerv(name, out)
    return out[0]


def _compile(kind: int, src: str) -> tuple[int, str, float]:
    """Return (shader_id_or_0, log, compile_ms). 0 = compile failed."""
    sh = gl.glCreateShader(kind)
    gl.glShaderSource(sh, src)
    t0 = time.perf_counter()
    gl.glCompileShader(sh)
    # Force the driver to actually finish the compile by querying status.
    status = gl.glGetShaderiv(sh, gl.GL_COMPILE_STATUS)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    log = gl.glGetShaderInfoLog(sh)
    log_str = log.decode() if isinstance(log, bytes) else str(log)
    if not status:
        gl.glDeleteShader(sh)
        return 0, log_str, elapsed_ms
    return sh, log_str, elapsed_ms


def _link(vs_src: str, fs_src: str) -> tuple[int, str, float]:
    """Return (program_id_or_0, log, total_compile_link_ms)."""
    t0 = time.perf_counter()
    vs, vs_log, _ = _compile(gl.GL_VERTEX_SHADER, vs_src)
    if not vs:
        return 0, f"VS:\n{vs_log}", (time.perf_counter() - t0) * 1000
    fs, fs_log, _ = _compile(gl.GL_FRAGMENT_SHADER, fs_src)
    if not fs:
        gl.glDeleteShader(vs)
        return 0, f"FS:\n{fs_log}", (time.perf_counter() - t0) * 1000
    prog = gl.glCreateProgram()
    gl.glAttachShader(prog, vs)
    gl.glAttachShader(prog, fs)
    gl.glLinkProgram(prog)
    status = gl.glGetProgramiv(prog, gl.GL_LINK_STATUS)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    log_raw = gl.glGetProgramInfoLog(prog)
    log = log_raw.decode() if isinstance(log_raw, bytes) else str(log_raw)
    gl.glDeleteShader(vs)
    gl.glDeleteShader(fs)
    if not status:
        gl.glDeleteProgram(prog)
        return 0, log, elapsed_ms
    return prog, log, elapsed_ms


# ---------- Shaders ----------

VS_BASIC = """\
attribute vec2 a_pos;
varying vec2 v_uv;
void main() {
  v_uv = a_pos * 0.5 + 0.5;
  gl_Position = vec4(a_pos, 0.0, 1.0);
}
"""

FS_TRIVIAL = """\
precision mediump float;
varying vec2 v_uv;
void main() { gl_FragColor = vec4(v_uv, 0.0, 1.0); }
"""

# Constant-index sampler array — should always work per spec.
FS_SAMPLER_ARRAY_CONST = """\
precision mediump float;
varying vec2 v_uv;
uniform sampler2D u_layers[4];
void main() {
  vec4 c = texture2D(u_layers[0], v_uv)
         + texture2D(u_layers[1], v_uv)
         + texture2D(u_layers[2], v_uv)
         + texture2D(u_layers[3], v_uv);
  gl_FragColor = c * 0.25;
}
"""

# Dynamic-index sampler array driven by a uniform int. The reviewer
# claimed vc4 doesn't support this. Let's measure.
FS_SAMPLER_ARRAY_DYNAMIC_UNIFORM = """\
precision mediump float;
varying vec2 v_uv;
uniform sampler2D u_layers[4];
uniform int u_idx;
void main() {
  gl_FragColor = texture2D(u_layers[u_idx], v_uv);
}
"""

# Dynamic-index sampler array driven by a varying-derived loop. The
# reviewer's prediction: this won't compile or will unroll.
FS_SAMPLER_ARRAY_DYNAMIC_LOOP = """\
precision mediump float;
varying vec2 v_uv;
uniform sampler2D u_layers[4];
uniform int u_count;
void main() {
  vec4 c = vec4(0.0);
  for (int i = 0; i < 4; ++i) {
    if (i >= u_count) break;
    c += texture2D(u_layers[i], v_uv);
  }
  gl_FragColor = c;
}
"""


def _fs_n_samplers(n: int) -> str:
    """Build a fragment shader that declares n samplers (each indexed
    only by its constant) and sums their samples. Used to test the
    actual MAX_TEXTURE_IMAGE_UNITS limit by linking N samplers."""
    decls = "\n".join(f"uniform sampler2D u_t{i};" for i in range(n))
    sums = "\n  + ".join(f"texture2D(u_t{i}, v_uv)" for i in range(n))
    return f"""\
precision mediump float;
varying vec2 v_uv;
{decls}
void main() {{
  vec4 c = {sums};
  gl_FragColor = c / float({n});
}}
"""


def _fs_n_samples_per_fragment(n: int) -> str:
    """Single texture, sampled n times at slightly-different uvs.
    Use this to measure raw texture-fetch bandwidth at 1080p."""
    samples = "\n  + ".join(
        f"texture2D(u_tex, v_uv + vec2({i*0.001}, {i*0.001}))" for i in range(n)
    )
    return f"""\
precision mediump float;
varying vec2 v_uv;
uniform sampler2D u_tex;
void main() {{
  vec4 c = {samples};
  gl_FragColor = c / float({n});
}}
"""


# ---------- Bandwidth probe setup ----------

def _make_test_texture(w: int, h: int) -> int:
    tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
    pixels = (ctypes.c_uint8 * (w * h * 4))()
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, w, h, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, pixels)
    return int(tex)


def _make_fbo(w: int, h: int) -> tuple[int, int]:
    """Return (fbo, color_tex)."""
    color_tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, color_tex)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, w, h, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
    fbo = gl.glGenFramebuffers(1)
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
    gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, color_tex, 0)
    status = gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER)
    if status != gl.GL_FRAMEBUFFER_COMPLETE:
        raise RuntimeError(f"FBO incomplete: 0x{status:04x}")
    return int(fbo), int(color_tex)


def _setup_quad() -> int:
    """Returns vbo id."""
    verts = (ctypes.c_float * 8)(-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0)
    vbo = gl.glGenBuffers(1)
    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
    gl.glBufferData(gl.GL_ARRAY_BUFFER, ctypes.sizeof(verts), verts, gl.GL_STATIC_DRAW)
    return int(vbo)


def _measure_frame_ms(prog: int, vbo: int, fbo: int, w: int, h: int,
                       textures: list[int], frames: int = 30) -> dict:
    """Run `frames` quad-fills with `textures` bound, return median + p99 frame ms."""
    gl.glUseProgram(prog)
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
    gl.glViewport(0, 0, w, h)
    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
    a_pos_loc = gl.glGetAttribLocation(prog, "a_pos")
    gl.glEnableVertexAttribArray(a_pos_loc)
    gl.glVertexAttribPointer(a_pos_loc, 2, gl.GL_FLOAT, gl.GL_FALSE, 0, None)

    for i, t in enumerate(textures):
        gl.glActiveTexture(gl.GL_TEXTURE0 + i)
        gl.glBindTexture(gl.GL_TEXTURE_2D, t)
    # If this is a single-sampler probe, set u_tex=0; if multi-sampler,
    # set each u_t<i>=i.
    loc = gl.glGetUniformLocation(prog, "u_tex")
    if loc >= 0:
        gl.glUniform1i(loc, 0)
    else:
        for i in range(len(textures)):
            sloc = gl.glGetUniformLocation(prog, f"u_t{i}")
            if sloc >= 0:
                gl.glUniform1i(sloc, i)

    # Warm-up to amortize first-draw shader compilation/linking lag.
    for _ in range(5):
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)
    gl.glFinish()

    times = []
    for _ in range(frames):
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
        t0 = time.perf_counter()
        gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)
        gl.glFinish()  # full pipeline drain so we measure GPU work, not enqueue
        times.append((time.perf_counter() - t0) * 1000)

    times.sort()
    return {
        "median_ms": times[len(times) // 2],
        "p99_ms": times[max(0, int(len(times) * 0.99) - 1)],
        "min_ms": times[0],
        "max_ms": times[-1],
    }


# ---------- DRM rotation property check ----------

def _drm_rotation_via_modetest() -> dict:
    """Use libdrm's modetest if available; otherwise read sysfs."""
    out: dict = {}
    try:
        # modetest is in libdrm-tests package; might not be on the Pi.
        r = subprocess.run(["modetest", "-M", "vc4", "-c"], capture_output=True, text=True, timeout=10)
        out["modetest_planes_returncode"] = r.returncode
        out["modetest_excerpt"] = r.stdout[:2000] if r.returncode == 0 else r.stderr[:500]
    except FileNotFoundError:
        out["modetest_planes_returncode"] = "modetest not installed"
    except Exception as e:
        out["modetest_planes_returncode"] = f"error: {e}"

    # Fallback: query sysfs for rotation property
    try:
        sysfs = Path("/sys/class/drm/card0-HDMI-A-1/")
        out["hdmi_connected"] = (sysfs / "status").read_text().strip() if sysfs.exists() else "no card0-HDMI-A-1"
    except Exception as e:
        out["hdmi_connected"] = f"error: {e}"

    return out


# ---------- Main ----------

def main() -> int:
    results: dict = {}

    print("setting up EGL+GBM context...", file=sys.stderr)
    display, context, surface, drm_fd, gbm_dev, gbm_surf = _setup_egl_context()
    try:
        # ---- 1. GL limits ----
        def _gl_str(name: int) -> str:
            v = gl.glGetString(name)
            if v is None:
                return ""
            if isinstance(v, bytes):
                return v.decode()
            # PyOpenGL on Pi returns GLubyteArray — cast through ctypes.
            try:
                return ctypes.cast(v, ctypes.c_char_p).value.decode()
            except Exception:
                return str(v)

        results["gl_info"] = {
            "vendor": _gl_str(gl.GL_VENDOR),
            "renderer": _gl_str(gl.GL_RENDERER),
            "version": _gl_str(gl.GL_VERSION),
            "glsl": _gl_str(gl.GL_SHADING_LANGUAGE_VERSION),
            "extensions": _gl_str(gl.GL_EXTENSIONS),
        }
        results["gl_limits"] = {
            "MAX_TEXTURE_IMAGE_UNITS": _gl_int(gl.GL_MAX_TEXTURE_IMAGE_UNITS),
            "MAX_VERTEX_TEXTURE_IMAGE_UNITS": _gl_int(gl.GL_MAX_VERTEX_TEXTURE_IMAGE_UNITS),
            "MAX_COMBINED_TEXTURE_IMAGE_UNITS": _gl_int(gl.GL_MAX_COMBINED_TEXTURE_IMAGE_UNITS),
            "MAX_FRAGMENT_UNIFORM_VECTORS": _gl_int(gl.GL_MAX_FRAGMENT_UNIFORM_VECTORS),
            "MAX_VERTEX_UNIFORM_VECTORS": _gl_int(gl.GL_MAX_VERTEX_UNIFORM_VECTORS),
            "MAX_VARYING_VECTORS": _gl_int(gl.GL_MAX_VARYING_VECTORS),
            "MAX_VERTEX_ATTRIBS": _gl_int(gl.GL_MAX_VERTEX_ATTRIBS),
            "MAX_TEXTURE_SIZE": _gl_int(gl.GL_MAX_TEXTURE_SIZE),
            "MAX_RENDERBUFFER_SIZE": _gl_int(gl.GL_MAX_RENDERBUFFER_SIZE),
        }

        # ---- 6. EGL extensions for dmabuf import ----
        ext_str = _libegl.eglQueryString(display, egl.EGL_EXTENSIONS) or b""
        ext_list = ext_str.decode().split() if isinstance(ext_str, bytes) else []
        results["egl_extensions"] = {
            "all": ext_list,
            "EGL_EXT_image_dma_buf_import": "EGL_EXT_image_dma_buf_import" in ext_list,
            "EGL_EXT_image_dma_buf_import_modifiers": "EGL_EXT_image_dma_buf_import_modifiers" in ext_list,
            "EGL_KHR_image_base": "EGL_KHR_image_base" in ext_list,
        }

        # ---- 2. Sampler array indexing modes ----
        results["sampler_array_indexing"] = {}
        for label, fs in [
            ("constant_index", FS_SAMPLER_ARRAY_CONST),
            ("uniform_index", FS_SAMPLER_ARRAY_DYNAMIC_UNIFORM),
            ("loop_index", FS_SAMPLER_ARRAY_DYNAMIC_LOOP),
        ]:
            prog, log, ms = _link(VS_BASIC, fs)
            results["sampler_array_indexing"][label] = {
                "linked": prog != 0,
                "compile_link_ms": round(ms, 2),
                "log_excerpt": log[:400],
            }
            if prog:
                gl.glDeleteProgram(prog)

        # ---- 3. Sampler unit count ----
        results["sampler_unit_count"] = {}
        for n in [4, 8, 12, 14, 16]:
            prog, log, ms = _link(VS_BASIC, _fs_n_samplers(n))
            results["sampler_unit_count"][f"n{n}"] = {
                "linked": prog != 0,
                "compile_link_ms": round(ms, 2),
                "log_excerpt": log[:300] if log else "",
            }
            if prog:
                gl.glDeleteProgram(prog)

        # ---- 4. Compile times for representative shaders ----
        results["shader_compile_times_ms"] = {}
        # Long branchy shader simulating the unified compositor path.
        long_shader = """\
precision mediump float;
varying vec2 v_uv;
uniform sampler2D u_a_bg;
uniform sampler2D u_a0; uniform sampler2D u_a1; uniform sampler2D u_a2;
uniform sampler2D u_b_bg;
uniform sampler2D u_b0; uniform sampler2D u_b1; uniform sampler2D u_b2;
uniform vec4 u_a_motion[3]; uniform vec4 u_a_geom[3]; uniform int u_a_blend[3];
uniform vec4 u_b_motion[3]; uniform vec4 u_b_geom[3]; uniform int u_b_blend[3];
uniform float u_t; uniform int u_kind; uniform vec3 u_lut;
vec3 blend_apply(vec3 base, vec3 top, int mode) {
  if (mode == 0) return top;
  else if (mode == 1) return base * top;
  else if (mode == 2) return base + top - base * top;
  else return mix(2.0*base*top, 1.0 - 2.0*(1.0-base)*(1.0-top), step(vec3(0.5), base));
}
void main() {
  vec4 a = texture2D(u_a_bg, v_uv);
  vec4 a0 = texture2D(u_a0, v_uv + u_a_motion[0].xy); a.rgb = blend_apply(a.rgb, a0.rgb, u_a_blend[0]);
  vec4 a1 = texture2D(u_a1, v_uv + u_a_motion[1].xy); a.rgb = blend_apply(a.rgb, a1.rgb, u_a_blend[1]);
  vec4 a2 = texture2D(u_a2, v_uv + u_a_motion[2].xy); a.rgb = blend_apply(a.rgb, a2.rgb, u_a_blend[2]);
  vec4 b = texture2D(u_b_bg, v_uv);
  vec4 b0 = texture2D(u_b0, v_uv + u_b_motion[0].xy); b.rgb = blend_apply(b.rgb, b0.rgb, u_b_blend[0]);
  vec4 b1 = texture2D(u_b1, v_uv + u_b_motion[1].xy); b.rgb = blend_apply(b.rgb, b1.rgb, u_b_blend[1]);
  vec4 b2 = texture2D(u_b2, v_uv + u_b_motion[2].xy); b.rgb = blend_apply(b.rgb, b2.rgb, u_b_blend[2]);
  vec3 mixed = mix(a.rgb, b.rgb, u_t);
  gl_FragColor = vec4(pow(mixed * u_lut.x, vec3(u_lut.y)), 1.0);
}
"""
        for label, fs in [
            ("trivial", FS_TRIVIAL),
            ("4_const_samplers", FS_SAMPLER_ARRAY_CONST),
            ("unified_8sampler_4blend", long_shader),
        ]:
            prog, _, ms = _link(VS_BASIC, fs)
            results["shader_compile_times_ms"][label] = round(ms, 2)
            if prog:
                gl.glDeleteProgram(prog)

        # ---- 5. Bandwidth probe at 1080p ----
        W, H = 1920, 1080
        print(f"setting up 1080p FBO + textures for bandwidth probe...", file=sys.stderr)
        fbo, _ = _make_fbo(W, H)
        vbo = _setup_quad()
        tex = _make_test_texture(W, H)

        results["bandwidth_1080p"] = {}
        for n in [1, 2, 4, 6, 8, 14]:
            try:
                prog, _, _ = _link(VS_BASIC, _fs_n_samples_per_fragment(n))
                if not prog:
                    results["bandwidth_1080p"][f"n{n}"] = {"error": "link failed"}
                    continue
                m = _measure_frame_ms(prog, vbo, fbo, W, H, [tex] * 1, frames=20)
                results["bandwidth_1080p"][f"n{n}"] = m
                gl.glDeleteProgram(prog)
            except Exception as e:
                results["bandwidth_1080p"][f"n{n}"] = {"error": str(e)}

        # ---- 7. glReadPixels stall cost ----
        prog, _, _ = _link(VS_BASIC, _fs_n_samples_per_fragment(1))
        gl.glUseProgram(prog)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
        gl.glViewport(0, 0, W, H)
        a_pos_loc = gl.glGetAttribLocation(prog, "a_pos")
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glEnableVertexAttribArray(a_pos_loc)
        gl.glVertexAttribPointer(a_pos_loc, 2, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
        loc = gl.glGetUniformLocation(prog, "u_tex")
        if loc >= 0:
            gl.glUniform1i(loc, 0)
        gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)
        gl.glFinish()
        readback_buf = (ctypes.c_uint8 * (W * H * 4))()
        readback_times = []
        for _ in range(5):
            gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)
            t0 = time.perf_counter()
            gl.glReadPixels(0, 0, W, H, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, readback_buf)
            readback_times.append((time.perf_counter() - t0) * 1000)
        readback_times.sort()
        results["glReadPixels_1080p_rgba_ms"] = {
            "median_ms": readback_times[len(readback_times) // 2],
            "min_ms": readback_times[0],
            "max_ms": readback_times[-1],
        }
        gl.glDeleteProgram(prog)

    finally:
        try:
            egl.eglMakeCurrent(display, egl.EGL_NO_SURFACE, egl.EGL_NO_SURFACE, egl.EGL_NO_CONTEXT)
            egl.eglDestroyContext(display, context)
            egl.eglDestroySurface(display, surface)
            egl.eglTerminate(display)
        except Exception:
            pass
        try:
            _gbm.gbm_surface_destroy(gbm_surf)
            _gbm.gbm_device_destroy(gbm_dev)
            os.close(drm_fd)
        except Exception:
            pass

    # ---- 8. DRM rotation / sysfs probe (no GL needed) ----
    results["drm"] = _drm_rotation_via_modetest()

    # ---- meminfo snapshot (CMA usage during this run) ----
    try:
        with open("/proc/meminfo") as f:
            mi = {}
            for line in f:
                k, _, v = line.partition(":")
                mi[k.strip()] = v.strip()
            results["meminfo"] = {
                k: mi[k] for k in ("MemTotal", "MemAvailable", "CmaTotal", "CmaFree", "Slab") if k in mi
            }
    except Exception as e:
        results["meminfo"] = {"error": str(e)}

    print(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
