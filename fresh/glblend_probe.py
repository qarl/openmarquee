#!/usr/bin/env python3
"""fresh/glblend_probe.py -- de-risk probe for custom GL shader blend.

Per QA dispatch 2026-06-18: glvideomixer / GstAggregator is a
confirmed dead end. The 8701765 instrument proved the aggregator
STOPS outputting after a re-prime (mix_out=0 when screen=0, pads
non-EOS) because a fresh source starts at running-time 0 while the
aggregator output is at T>0 -> its buffers are dropped as "past"
-> the aggregator waits forever. The way forward is a CUSTOM GL
SHADER BLEND with NO aggregator -- we do the 2-texture mix
ourselves so there's no wait-on-all-pads, no running-time
alignment, no idle-pad starve.

BEFORE the full reel rewrite this probe proves the SINGLE RISKIEST
ASSUMPTION: that a GstGLMemory texture pulled from an appsink can
be handed to a separate appsrc in a SHARED GL CONTEXT, run through
a custom GLSL fragment shader (in this probe via the gst-plugins-
bad `glshader` element, which exercises the same GL-context-share
plumbing that a hand-rolled FBO render would), and presented via
glimagesink at 24fps on this V3D Pi.

ARCHITECTURE:

  Input pipeline:
    videotestsrc num-buffers=480 ! NV12 1280x720 24fps ! glupload
    ! glcolorconvert ! appsink (caps memory:GLMemory RGBA)
                              |
                              v  (42ms GLib.timeout tick:
                                  try_pull_sample(0) + push-buffer)
                              v
  Output pipeline:
    appsrc (memory:GLMemory RGBA 1280x720 24fps) ! glshader
    (single-tap custom fragment: gl_FragColor = texture2D(
    u_cur, v_uv)) ! glimagesink sync=false

  SHARED CONTEXT: one GstGLDisplay + one GstGLContext for the
  whole process, wired into both pipelines via a SYNC bus
  handler that answers need-context for gst.gl.GLDisplay and
  gst.gl.app_context. Without this, gst-gl creates a separate
  context per pipeline and the input GLMemory is invalid in the
  output context -> black/garbage output and the probe FAILS.
  This is the load-bearing part.

WHAT IT PROVES:
  - Shared-context plumbing works (the appsink GLMemory is valid
    in the appsrc/glshader/glimagesink path).
  - A custom GLSL fragment shader can be applied to a GLMemory
    texture in our shared context at 24fps on V3D.
  - The HDMI presentation chain is solid (glimagesink does NOT
    need an aggregator).
  If all three hold, the full crossfade reel is mechanical:
  reuse cutloop's concat per branch + a 2-tap mix shader (a
  ~5-line fragment) + the state machine -- with NO aggregator,
  NO wait-on-all-pads, NO running-time alignment.

WHAT IT DOES NOT PROVE:
  Doing the FBO render + shader ourselves OUTSIDE the gst
  element framework (i.e. on GstGL.GLContext.thread_add with our
  own glBindFramebuffer + glDrawArrays). The `glshader` element
  performs the same underlying GL ops behind a thin wrapper, so
  if this probe works, doing them in our own thread_add callbacks
  is a strictly easier problem (same GL semantics, less coordin-
  ation overhead). QA explicitly designed this as a cheap de-risk
  for the broad assumption; the strict "we own the render"
  version is a follow-up step if this one passes.

SUCCESS CRITERIA on glass:
  - The videotestsrc test pattern (color bars + ball) is VISIBLE
    on the HDMI for the full ~20s (480 frames at 24fps).
  - [probe] screen ~= 24 each second, sustained until EOS.
  - CmaFree stable >50MB throughout.
  - No GL/EGL errors on stderr.

DO NOT TOUCH cutfade.py or cutloop.py. cutloop stays the sign
default. QA deploys + runs the probe via a transient unit, then
restores cutloop.
"""

import os
import sys
import time

# GL env MUST be set before Gst.init.
os.environ.setdefault("GST_GL_PLATFORM", "egl")
os.environ.setdefault("GST_GL_WINDOW", "gbm")
os.environ.setdefault("GST_GL_API", "gles2")


def _ensure_xdg_runtime_dir():
    """systemd transient units launched without logind get no
    XDG_RUNTIME_DIR. Mesa vc4 EGL/GBM uses it for the dri socket
    path and the shader cache; without it cold-compile blows
    preroll past 10s. Mirrors cutfade/cutloop's pattern."""
    if os.environ.get("XDG_RUNTIME_DIR"):
        return
    uid = os.getuid()
    for candidate in (f"/run/user/{uid}", f"/tmp/runtime-{uid}"):
        try:
            os.makedirs(candidate, mode=0o700, exist_ok=True)
            os.chmod(candidate, 0o700)
            os.environ["XDG_RUNTIME_DIR"] = candidate
            print(f"[probe] XDG_RUNTIME_DIR fallback: {candidate}",
                  file=sys.stderr)
            return
        except OSError:
            continue


_ensure_xdg_runtime_dir()


import gi  # noqa: E402

gi.require_version("Gst", "1.0")
gi.require_version("GstGL", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gst, GstGL  # noqa: E402

Gst.init(None)
print("[probe] Gst initialized; GST_GL_PLATFORM=egl GST_GL_WINDOW=gbm "
      "GST_GL_API=gles2", file=sys.stderr)


def die(msg, code=1):
    print(f"[probe] FATAL {msg}", file=sys.stderr)
    sys.exit(code)


# --- Shared GL context -------------------------------------------------
# Create the ONE GLDisplay + GLContext for the whole process. Both
# pipelines (input + output) discover and adopt these via the SYNC
# bus handler below; without that wiring gst-gl creates a private
# context per pipeline and the cross-pipeline GLMemory handoff
# produces black/garbage on screen.
gl_display = GstGL.GLDisplay.new()
if gl_display is None:
    die("GstGL.GLDisplay.new() returned None")
gl_context = GstGL.GLContext.new(gl_display)
if gl_context is None:
    die("GstGL.GLContext.new() returned None")
if not gl_context.create(None):
    die("gl_context.create() failed (EGL/GBM unavailable?)")
print(f"[probe] shared GLDisplay + GLContext created", file=sys.stderr)


# --- Bus sync handler for need-context --------------------------------

def _on_sync_message(_bus, msg, _user_data):
    if msg.type != Gst.MessageType.NEED_CONTEXT:
        return Gst.BusSyncReply.PASS
    ok, ctx_type = msg.parse_context_type()
    if not ok:
        return Gst.BusSyncReply.PASS
    src_name = msg.src.get_name() if msg.src is not None else "?"
    if ctx_type == "gst.gl.GLDisplay":
        ctx = Gst.Context.new("gst.gl.GLDisplay", True)
        GstGL.context_set_gl_display(ctx, gl_display)
        if msg.src is not None:
            msg.src.set_context(ctx)
        print(f"[probe] provided GLDisplay to {src_name}",
              file=sys.stderr)
    elif ctx_type == "gst.gl.app_context":
        ctx = Gst.Context.new("gst.gl.app_context", True)
        s = ctx.writable_structure()
        s.set_value("context", gl_context)
        # Verify the round-trip: in some PyGObject versions
        # set_value on a generic GstStructure field with a
        # GObject value can silently no-op, leading to separate
        # per-pipeline contexts and a black screen with NO error.
        # Loud-fail at startup so QA's soak immediately spots it.
        roundtrip = s.get_value("context")
        if roundtrip is None:
            print(f"[probe] FATAL app_context set_value("
                  "'context', gl_context) did not round-trip "
                  "(GValue marshalling failed); shared-context "
                  "wiring is BROKEN -- expect black output. "
                  "Aborting now rather than discovering on glass.",
                  file=sys.stderr)
            sys.exit(2)
        if msg.src is not None:
            msg.src.set_context(ctx)
        print(f"[probe] provided app_context to {src_name} "
              f"(round-trip ok={roundtrip is not None})",
              file=sys.stderr)
    return Gst.BusSyncReply.PASS


# --- Input pipeline: videotestsrc -> appsink --------------------------

GLMEM_CAPS = (
    'video/x-raw(memory:GLMemory),format=RGBA,'
    'width=1280,height=720,framerate=24/1'
)

IN_DESC = (
    "videotestsrc name=src num-buffers=480 ! "
    "video/x-raw,format=NV12,width=1280,height=720,framerate=24/1 ! "
    "glupload ! glcolorconvert ! "
    f"appsink name=in caps=\"{GLMEM_CAPS}\" "
    "emit-signals=false max-buffers=2 drop=true sync=false"
)
in_pipeline = Gst.parse_launch(IN_DESC)
if in_pipeline is None:
    die("input pipeline parse_launch failed")
in_pipeline.get_bus().set_sync_handler(_on_sync_message, None)
appsink = in_pipeline.get_by_name("in")
if appsink is None:
    die("input pipeline: appsink 'in' not found")


# --- Output pipeline: appsrc -> glshader -> glimagesink ---------------
#
# Single-tap fragment per QA dispatch: samples u_cur (the input
# texture) at v_uv and writes it out. glshader's default vertex
# shader provides a fullscreen-quad with v_uv as the varying.

FRAG_SRC = (
    "#ifdef GL_ES\n"
    "precision mediump float;\n"
    "#endif\n"
    "varying vec2 v_uv;\n"
    "uniform sampler2D u_cur;\n"
    "void main(void) {\n"
    "  gl_FragColor = texture2D(u_cur, v_uv);\n"
    "}\n"
)

OUT_DESC = (
    f"appsrc name=src is-live=true format=time do-timestamp=true "
    f"caps=\"{GLMEM_CAPS}\" ! "
    "glshader name=shader ! "
    "glimagesink name=sink sync=false"
)
out_pipeline = Gst.parse_launch(OUT_DESC)
if out_pipeline is None:
    die("output pipeline parse_launch failed")
out_pipeline.get_bus().set_sync_handler(_on_sync_message, None)
appsrc = out_pipeline.get_by_name("src")
shader = out_pipeline.get_by_name("shader")
glsink = out_pipeline.get_by_name("sink")
if appsrc is None or shader is None or glsink is None:
    die("output pipeline: appsrc/shader/sink not found")
shader.set_property("fragment", FRAG_SRC)
# DO NOT set vertex to "" -- on gst 1.22 the empty string can
# trigger "no vertex shader" instead of falling back to the
# built-in fullscreen-quad vertex. Leave the property at its
# default per glshader docs.


# --- Counters + sink probe ---------------------------------------------
pulled = [0]
pushed = [0]
screen_frames = [0]

sink_pad = glsink.get_static_pad("sink")
if sink_pad is None:
    die("glimagesink has no sink pad")


def _on_sink_buf(_pad, _info):
    screen_frames[0] += 1
    return Gst.PadProbeReturn.OK


sink_pad.add_probe(Gst.PadProbeType.BUFFER, _on_sink_buf)


# --- Render tick: pull sample -> push to appsrc -----------------------

def _render_tick():
    sample = appsink.try_pull_sample(0)
    if sample is None:
        return True
    pulled[0] += 1
    buf = sample.get_buffer()
    if buf is None:
        return True
    ret = appsrc.emit("push-buffer", buf)
    if ret == Gst.FlowReturn.OK:
        pushed[0] += 1
    else:
        print(f"[probe] appsrc push-buffer returned {ret}",
              file=sys.stderr)
    return True


# --- 1Hz instrument ----------------------------------------------------

def _cma_free_kb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("CmaFree:"):
                    return int(line.split()[1])
    except Exception:
        return -1
    return -1


_t_start = time.monotonic()


def _instrument():
    uptime = int(time.monotonic() - _t_start)
    print(f"[probe] t={uptime} screen={screen_frames[0]} "
          f"pulled={pulled[0]} pushed={pushed[0]} "
          f"CmaFree_kB={_cma_free_kb()}",
          file=sys.stderr, flush=True)
    screen_frames[0] = 0
    pulled[0] = 0
    pushed[0] = 0
    return True


# --- Bus error/EOS watchers (async signal watch) ----------------------

loop = GLib.MainLoop()


def _on_async_message(_bus, msg, label):
    if msg.type == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        src = msg.src.get_name() if msg.src is not None else "?"
        print(f"[probe] {label} ERROR {src}: {err.message}",
              file=sys.stderr)
        if dbg:
            print(f"[probe]  debug: {dbg}", file=sys.stderr)
        loop.quit()
    elif msg.type == Gst.MessageType.EOS:
        src = msg.src.get_name() if msg.src is not None else "?"
        print(f"[probe] {label} EOS on {src}", file=sys.stderr)
        if label == "in":
            # videotestsrc EOSed after 480 buffers. appsink will
            # not pull anything new; forward EOS to appsrc so the
            # output pipeline drains its remaining buffers and
            # cleanly EOSes through glimagesink. Without this the
            # output pipeline idles indefinitely.
            print("[probe] forwarding EOS to appsrc",
                  file=sys.stderr)
            appsrc.emit("end-of-stream")
        elif label == "out":
            # Output EOS = glimagesink finished. We're done.
            loop.quit()


for pipe, label in ((in_pipeline, "in"), (out_pipeline, "out")):
    bus = pipe.get_bus()
    bus.add_signal_watch()
    bus.connect("message", _on_async_message, label)


# --- Run ---------------------------------------------------------------

print("[probe] starting input pipeline", file=sys.stderr)
if (in_pipeline.set_state(Gst.State.PLAYING)
        == Gst.StateChangeReturn.FAILURE):
    die("input pipeline set_state PLAYING failed")
print("[probe] starting output pipeline", file=sys.stderr)
if (out_pipeline.set_state(Gst.State.PLAYING)
        == Gst.StateChangeReturn.FAILURE):
    die("output pipeline set_state PLAYING failed")

GLib.timeout_add(42, _render_tick)
GLib.timeout_add(1000, _instrument)


try:
    print("[probe] entering main loop", file=sys.stderr)
    loop.run()
finally:
    print("[probe] shutdown -> NULL", file=sys.stderr)
    in_pipeline.set_state(Gst.State.NULL)
    out_pipeline.set_state(Gst.State.NULL)
    print("[probe] done", file=sys.stderr)
