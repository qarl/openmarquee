#!/usr/bin/env python3
"""fresh/wipe.py — A <-> B wipe with TWO live HW H264 decoders, GL blend, seek-loop.

Step 2b (service-robust iteration) of fresh-stack rebuild (2026-06-17).

v3 changes on top of 8099590 (service launch hardening):

  - XDG_RUNTIME_DIR fallback. systemd-run --scope launches inherit
    NO XDG_RUNTIME_DIR (no logind = no /run/user/<uid>). Mesa's vc4
    EGL/GBM init uses XDG_RUNTIME_DIR for dri-socket path AND for
    shader-cache discovery; without it, first GL context creation
    cold-compiles every shader and the PAUSED preroll runs past 10s.
    `_ensure_xdg_runtime_dir()` populates the env BEFORE Gst.init,
    falling back from inherited -> /run/user/<uid> -> /tmp/runtime-<uid>
    (created 0700 if missing).
  - 30s preroll budget with progress polling. The old 5s timeout died
    immediately on the FIRST systemd cold-start (no cache, slower init);
    the new loop polls get_state at 1s ticks up to 30s, logs each
    ASYNC tick so a slow-but-progressing preroll is NOT mistaken for
    hang, and only dies on FAILURE or budget exhausted.
  - SIGTERM during preroll exits cleanly. The shutdown handler sets a
    shared `shutdown_requested` flag; the preroll polling loop checks
    it each iteration and exits without waiting for the systemd
    SIGTERM->SIGKILL timer.

v2 changes (kept from 8099590):

  1. GPU blend: glupload -> glvideomixer -> gldownload -> kmssink
     (was: videoconvert -> compositor [CPU sysmem] -> kmssink, which
     QA measured as frame-dropping with CPU climbing 43%->95% backlog).
     glupload imports the v4l2h264dec dmabuf as an EGL image (zero-copy
     on V3D/Mesa); glvideomixer blends two GL textures via shader;
     gldownload pulls the composited RGB into sysmem once per frame for
     kmssink. Sysmem cost is now one 1080p readback per output frame
     instead of two 1080p YUV downloads + a CPU blend per frame.

  2. Continuous loop without decoder teardown: named filesrc + qtdemux
     per source. On entry to PAUSED we issue a non-flushing SEGMENT seek
     on each demuxer (start=0, stop=NONE = play to end). The demuxer
     emits a SEGMENT_DONE bus message at end-of-stream instead of EOS;
     the bus handler issues another SEGMENT seek back to time 0. NO
     FLUSH event reaches v4l2h264dec at the loop boundary, so the
     gstreamer v4l2videodec flush handler does NOT call
     VIDIOC_STREAMOFF/STREAMON on the CAPTURE queue — the bcm2835-codec
     REQBUFS/EINVAL wedge surface is NOT exercised by the loop.
     (A prior version using `multifilesrc loop=true` EOS-quit at ~14s
     because qtdemux forwards EOS rather than looping; a flushing-seek
     version was rejected because flush would trigger STREAMOFF.)

Sibling to play.sh; play.sh stays single-pipeline sequential.

EFFECT: left-to-right wipe of the incoming video over the outgoing one.
glvideomixer sink_1 pad geometry (xpos=0, width animated 0->1920 over
WIPE_S) reveals the incoming on top of the outgoing (z-order 1 > 0).
Roles swap after each wipe; both decoders run continuously.

CORE STUDY (per QA dispatch 2026-06-17): two simultaneous HW decoders
on bcm2835 is the freeze surface. This script proves the wipe +
dual-decode lifecycle works; the "warm the incoming decoder early"
knob is still the NEXT step. Marked TODO(warm) below.

HW-decode + GL-path verification on the Pi:
    v4l2-ctl --list-devices                          # /dev/video10
    fuser -v /dev/video10                            # two opens (one per decoder)
    GST_DEBUG=v4l2:5,glupload:4 ./wipe.py 2>&1 \
        | grep -iE 'open.*video1[01]|dmabuf|EGL'

GL platform: defaults to EGL on GBM with GLES2 (right path for V3D on
headless Pi). Override via the standard GST_GL_* env vars if needed.
"""

import os
import signal
import subprocess
import sys

# Headless GL on V3D + KMS/GBM. Set BEFORE Gst.init so gstreamer-gl picks
# the GBM window backend (EGL on dri/card0) instead of trying X11/Wayland.
os.environ.setdefault("GST_GL_PLATFORM", "egl")
os.environ.setdefault("GST_GL_WINDOW", "gbm")
os.environ.setdefault("GST_GL_API", "gles2")


def _ensure_xdg_runtime_dir():
    """systemd transient services launched without logind (e.g.
    `systemd-run --scope`) inherit NO XDG_RUNTIME_DIR. EGL/GBM on Mesa
    uses XDG_RUNTIME_DIR for the dri socket path AND for the shader
    cache (XDG_CACHE_HOME falls back via XDG_RUNTIME_DIR derivations).
    Without it the first GL context creation on V3D triggers a full
    cold shader compile and various probe fallbacks — empirically
    pushing PAUSED preroll past 10s on Pi Zero 2 W.

    Fallback chain: existing env -> /run/user/<uid> (logind path) ->
    /tmp/runtime-<uid> (mode 0700, created if missing).
    """
    if os.environ.get("XDG_RUNTIME_DIR"):
        return
    uid = os.getuid()
    for candidate in (f"/run/user/{uid}", f"/tmp/runtime-{uid}"):
        try:
            os.makedirs(candidate, mode=0o700, exist_ok=True)
            os.chmod(candidate, 0o700)
            os.environ["XDG_RUNTIME_DIR"] = candidate
            print(f"[wipe] XDG_RUNTIME_DIR fallback: {candidate}")
            return
        except OSError:
            continue
    print("[wipe] WARN: no writable XDG_RUNTIME_DIR; GL cold-start may be slow",
          file=sys.stderr)


_ensure_xdg_runtime_dir()

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gst  # noqa: E402

VIDEOS = [
    "/var/openmarquee/content/029c4d68-744c-4d30-9adc-0f37c55514f1/asset.mp4",
    "/var/openmarquee/content/3f54a4d2-a120-4c0c-aa80-5b99aaf7c9ff/asset.mp4",
]
W, H = 1920, 1080
HOLD_S = 4.0      # full-screen hold between wipes
WIPE_S = 1.0      # wipe duration
TICK_MS = 33      # geometry animation tick (~30fps)


def die(msg, code=1):
    print(f"[wipe] {msg}", file=sys.stderr)
    sys.exit(code)


# --- Pre-flights --------------------------------------------------------

for v in VIDEOS:
    if not os.path.exists(v):
        die(f"missing video: {v}")

Gst.init(None)

for el in (
    "filesrc", "qtdemux", "h264parse", "v4l2h264dec",
    "glupload", "glcolorconvert", "glvideomixer", "gldownload",
    "videoconvert", "kmssink",
):
    if not Gst.ElementFactory.find(el):
        die(f"missing gstreamer element: {el} "
            "(try sudo apt install gstreamer1.0-plugins-{good,bad} "
            "gstreamer1.0-gl)")

# Single HW decoder context — refuse double-start.
self_pid = os.getpid()
ps = subprocess.run(
    ["pgrep", "-af", "v4l2h264dec|openmarquee-render|mini-play"],
    capture_output=True, text=True,
)
peers = [
    line for line in ps.stdout.splitlines()
    if line.split() and line.split()[0] != str(self_pid)
]
if peers:
    die("another HW-decode process is already running:\n  "
        + "\n  ".join(peers))

# --- Pipeline -----------------------------------------------------------
#
# Two decode branches into glvideomixer; output via gldownload + kmssink.
# Named filesrc + qtdemux per branch so we can attach an EOS-loop probe
# on each demuxer's dynamic video pad.

pipe_desc = (
    f"glvideomixer name=mix background=black "
    f"  sink_0::xpos=0 sink_0::ypos=0 "
    f"  sink_0::width={W} sink_0::height={H} sink_0::zorder=0 "
    f"  sink_1::xpos=0 sink_1::ypos=0 "
    f"  sink_1::width=0 sink_1::height={H} sink_1::zorder=1 "
    f"! gldownload ! videoconvert ! kmssink sync=true "
    f'filesrc location="{VIDEOS[0]}" name=srcA '
    f"  ! qtdemux name=demuxA ! h264parse ! v4l2h264dec "
    f"  ! glupload ! glcolorconvert ! mix.sink_0 "
    f'filesrc location="{VIDEOS[1]}" name=srcB '
    f"  ! qtdemux name=demuxB ! h264parse ! v4l2h264dec "
    f"  ! glupload ! glcolorconvert ! mix.sink_1"
)

try:
    pipeline = Gst.parse_launch(pipe_desc)
except GLib.Error as exc:
    die(f"pipeline parse failed: {exc.message}")

mix = pipeline.get_by_name("mix")
if mix is None:
    die("glvideomixer not found in parsed pipeline")


def find_sink_pad(element, name):
    """glvideomixer pads are request pads; get_static_pad behavior on
    request pads is version-dependent. Iterate sink pads to find by
    name reliably."""
    pad = element.get_static_pad(name)
    if pad is not None:
        return pad
    it = element.iterate_sink_pads()
    while True:
        res, p = it.next()
        if res != Gst.IteratorResult.OK:
            return None
        if p.get_name() == name:
            return p


pads = [find_sink_pad(mix, "sink_0"), find_sink_pad(mix, "sink_1")]
if pads[0] is None or pads[1] is None:
    die(f"could not locate glvideomixer pads sink_0/sink_1: {pads}")

# --- Per-source SEGMENT-seek loop ---------------------------------------
#
# After PAUSED, we issue a non-flushing SEGMENT seek on each demuxer.
# When the segment completes, the demuxer posts a SEGMENT_DONE message
# (not EOS) on the bus; the bus handler issues another SEGMENT seek to
# time 0. No FLUSH event ever propagates to v4l2h264dec, so no
# STREAMOFF/STREAMON cycle on the bcm2835-codec CAPTURE queue.

DEMUX_NAMES = ("demuxA", "demuxB")
demuxes = {}
for name in DEMUX_NAMES:
    demux = pipeline.get_by_name(name)
    if demux is None:
        die(f"{name} not found in parsed pipeline")
    demuxes[name] = demux


def segment_seek(elem):
    """Non-flushing segment seek from 0 to end (NONE)."""
    return elem.seek(
        1.0,                              # rate
        Gst.Format.TIME,
        Gst.SeekFlags.SEGMENT | Gst.SeekFlags.KEY_UNIT,
        Gst.SeekType.SET, 0,             # start
        Gst.SeekType.NONE, 0,            # stop (NONE = duration)
    )

# --- Sanity: exactly two HW decoders ------------------------------------

hw_decs = []
it = pipeline.iterate_elements()
while True:
    res, el = it.next()
    if res != Gst.IteratorResult.OK:
        break
    fac = el.get_factory()
    if fac and fac.get_name() == "v4l2h264dec":
        hw_decs.append(el.get_name())
print(f"[wipe] HW decoders in pipeline: {len(hw_decs)} -> {hw_decs}")
if len(hw_decs) != 2:
    die("expected exactly 2 v4l2h264dec instances")

# --- State machine ------------------------------------------------------

outgoing = 0
incoming = 1
state = "HOLD"
phase_start_ns = 0


def now_ns():
    return GLib.get_monotonic_time() * 1000  # us -> ns


def set_geom(pad, x, w):
    pad.set_property("xpos", int(x))
    pad.set_property("width", int(w))


def start_wipe():
    global state, phase_start_ns
    # TODO(warm): pre-emit a frame from the incoming pad ~150-300ms BEFORE
    # geometry animation begins, so the v4l2h264dec context is past first
    # CAPTURE-buffer allocation when the wipe edge starts moving. With the
    # EOS-seek loop both decoders stay in PLAYING continuously, so warming
    # should already be implicit -- but if the wipe seam stutters on glass,
    # the knob site is: temporarily zorder=1 + width=1 on the incoming pad
    # for ~200ms before animation start (single-pixel reveal warm-up).
    pads[incoming].set_property("zorder", 1)
    pads[outgoing].set_property("zorder", 0)
    state = "WIPE"
    phase_start_ns = now_ns()
    print(f"[wipe] wipe {outgoing} -> {incoming} begin")


def finish_wipe():
    global state, phase_start_ns, outgoing, incoming
    set_geom(pads[incoming], 0, W)
    set_geom(pads[outgoing], 0, 0)
    outgoing, incoming = incoming, outgoing
    state = "HOLD"
    phase_start_ns = now_ns()
    print(f"[wipe] hold {outgoing}")


def tick():
    global state
    elapsed_s = (now_ns() - phase_start_ns) / 1e9
    if state == "HOLD":
        if elapsed_s >= HOLD_S:
            start_wipe()
    elif state == "WIPE":
        if elapsed_s >= WIPE_S:
            finish_wipe()
        else:
            set_geom(pads[incoming], 0, (elapsed_s / WIPE_S) * W)
    return True  # keep firing


set_geom(pads[0], 0, W)
set_geom(pads[1], 0, 0)
phase_start_ns = now_ns()

# --- Bus + signals ------------------------------------------------------

loop = GLib.MainLoop()


def on_bus(_bus, msg):
    if msg.type == Gst.MessageType.SEGMENT_DONE:
        # End of a SEGMENT seek — re-arm to time 0 to keep this source
        # looping. No flush -> no STREAMOFF on bcm2835 v4l2h264dec.
        src = msg.src
        name = src.get_name() if src else "?"
        if name in DEMUX_NAMES:
            if not segment_seek(src):
                print(f"[wipe] {name} segment re-seek FAILED",
                      file=sys.stderr)
        return
    if msg.type == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        src = msg.src.get_name() if msg.src else "?"
        print(f"[wipe] ERROR {src}: {err.message}", file=sys.stderr)
        if dbg:
            print(f"[wipe]  debug: {dbg}", file=sys.stderr)
        loop.quit()
        return
    if msg.type == Gst.MessageType.EOS:
        # SEGMENT seeks should produce SEGMENT_DONE not EOS — if EOS
        # reaches the bus, something fell out of segment mode.
        print("[wipe] pipeline EOS reached bus (segment mode dropped?)",
              file=sys.stderr)
        loop.quit()


bus = pipeline.get_bus()
bus.add_signal_watch()
bus.connect("message", on_bus)


shutdown_requested = False


def shutdown(*_):
    global shutdown_requested
    shutdown_requested = True
    # The GLib main loop may not be running yet (still in preroll); the
    # preroll polling loop also checks shutdown_requested so SIGTERM
    # during cold-start exits cleanly without waiting on systemd SIGKILL.
    GLib.idle_add(loop.quit)


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

# --- Run ----------------------------------------------------------------

print(f"[wipe] starting; HOLD={HOLD_S}s WIPE={WIPE_S}s; ctrl-c to stop")

# PAUSED first so each demuxer parses its mp4 moov, exposes its video
# pad, and the downstream link to glupload/glcolorconvert/glvideomixer
# negotiates. Only then can we issue the SEGMENT seek that arms the
# non-flushing loop mode for each source.
if pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
    die("pipeline failed to enter PAUSED")

# GL cold-start as a systemd service (no logind session) legitimately
# needs 10-20s on a Zero 2 W even with the XDG_RUNTIME_DIR fallback
# above. Poll get_state with a 1s tick up to a 30s budget; log
# progress so a slow-but-progressing preroll isn't mistaken for hang.
PREROLL_BUDGET_S = 30
POLL_S = 1
prerolled = False
for elapsed in range(POLL_S, PREROLL_BUDGET_S + POLL_S, POLL_S):
    if shutdown_requested:
        pipeline.set_state(Gst.State.NULL)
        sys.exit(0)
    ret, cur, pending = pipeline.get_state(POLL_S * Gst.SECOND)
    if ret == Gst.StateChangeReturn.SUCCESS:
        print(f"[wipe] preroll done after ~{elapsed}s "
              f"(state={cur.value_nick})")
        prerolled = True
        break
    if ret == Gst.StateChangeReturn.FAILURE:
        die("pipeline preroll FAILURE")
    # ASYNC — still progressing; log + keep polling.
    print(f"[wipe] preroll... state={cur.value_nick} "
          f"pending={pending.value_nick} ({elapsed}/{PREROLL_BUDGET_S}s)")

if not prerolled:
    die(f"pipeline preroll exceeded {PREROLL_BUDGET_S}s budget")

for name, demux in demuxes.items():
    if not segment_seek(demux):
        die(f"initial SEGMENT seek failed on {name}")

if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
    die("pipeline failed to enter PLAYING")

GLib.timeout_add(TICK_MS, tick)
try:
    loop.run()
finally:
    pipeline.set_state(Gst.State.NULL)
