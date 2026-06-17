#!/usr/bin/env python3
"""fresh/wipe.py — A <-> B wipe with TWO live HW H264 decoders.

Step 2b of fresh-stack rebuild (2026-06-17). Same gstreamer stack as
step 2a, two of them: v4l2h264dec (bcm2835 HW H264) per source, into a
gst `compositor`, out via kmssink. Sibling to play.sh; play.sh stays
single-pipeline + sequential, wipe.py is the dual-decode/compositor.

EFFECT: left-to-right wipe of the incoming video over the outgoing one.
Compositor sink_1 pad geometry (xpos=0, width animated 0→1920 over
WIPE_S) reveals the incoming on top of the outgoing (z-order 1 > 0).
After the wipe completes, roles swap; both pads continue decoding their
sources via `multifilesrc loop=true` so neither decoder dies between
wipes (motion-through-transition requirement).

CORE STUDY (per QA dispatch 2026-06-17): two simultaneous HW decoders
on bcm2835 is the freeze surface. This script proves the wipe +
dual-decode lifecycle works; the "warm the incoming decoder early"
knob is the NEXT step. Marked TODO(warm) below.

HW-decode verification: this script enumerates v4l2h264dec elements
at startup. To confirm both are actually on HW (not silent fallback to
avdec_h264), on the Pi run:
    v4l2-ctl --list-devices         # bcm2835-codec-decode -> /dev/video10
    GST_DEBUG=v4l2:5 ./wipe.py 2>&1 | grep -i 'open.*video1[01]'

LIMITATIONS at this step (acceptable per spec):
- `compositor` is CPU (sysmem path); during the wipe both 1080p streams
  download from dmabuf -> RGB blend. May tank fps during the wipe
  window. Measurement on glass decides whether to move to glvideomixer.
- multifilesrc loop=true on a single .mp4: known to work for raw streams
  but qtdemux re-parsing on each loop may stutter at the loop seam.
  Acceptable for this study (seam smoothing is the NEXT step's job).
"""

import os
import signal
import subprocess
import sys

import gi

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
    "compositor", "videoconvert", "multifilesrc", "kmssink",
):
    if not Gst.ElementFactory.find(el):
        die(f"missing gstreamer element: {el} "
            "(try sudo apt install gstreamer1.0-plugins-{good,bad})")

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

pipe_desc = (
    f"compositor name=comp background=black "
    f"  sink_0::xpos=0 sink_0::ypos=0 "
    f"  sink_0::width={W} sink_0::height={H} sink_0::zorder=0 "
    f"  sink_1::xpos=0 sink_1::ypos=0 "
    f"  sink_1::width=0 sink_1::height={H} sink_1::zorder=1 "
    f"! videoconvert ! kmssink sync=true "
    f'multifilesrc location="{VIDEOS[0]}" loop=true ! qtdemux ! h264parse '
    f"  ! v4l2h264dec ! videoconvert ! comp.sink_0 "
    f'multifilesrc location="{VIDEOS[1]}" loop=true ! qtdemux ! h264parse '
    f"  ! v4l2h264dec ! videoconvert ! comp.sink_1"
)

try:
    pipeline = Gst.parse_launch(pipe_desc)
except GLib.Error as exc:
    die(f"pipeline parse failed: {exc.message}")

comp = pipeline.get_by_name("comp")
if comp is None:
    die("compositor not found in parsed pipeline")


def find_sink_pad(element, name):
    """compositor pads are request pads; get_static_pad behavior on them
    is version-dependent. Iterate sink pads to find by name reliably."""
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


pads = [find_sink_pad(comp, "sink_0"), find_sink_pad(comp, "sink_1")]
if pads[0] is None or pads[1] is None:
    die(f"could not locate compositor pads sink_0/sink_1: {pads}")

# Enumerate HW-decode elements so the operator can confirm two of them.
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
    # CAPTURE-buffer allocation when the wipe edge starts moving. With
    # multifilesrc loop=true both decoders run continuously throughout, so
    # warming should already be implicit -- but if the wipe seam stutters
    # on glass, this is the knob: temporarily zorder=1 on the incoming pad
    # for ~200ms with width=1 (single-pixel reveal) before animation start.
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
    if msg.type == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        src = msg.src.get_name() if msg.src else "?"
        print(f"[wipe] ERROR {src}: {err.message}", file=sys.stderr)
        if dbg:
            print(f"[wipe]  debug: {dbg}", file=sys.stderr)
        loop.quit()
    elif msg.type == Gst.MessageType.EOS:
        print("[wipe] pipeline EOS (unexpected with multifilesrc loop=true)",
              file=sys.stderr)
        loop.quit()


bus = pipeline.get_bus()
bus.add_signal_watch()
bus.connect("message", on_bus)


def shutdown(*_):
    # Signal context — defer pipeline teardown to the GLib main loop to
    # avoid GStreamer-from-signal-handler races. loop.quit() exits run(),
    # the finally clause does the NULL transition.
    GLib.idle_add(loop.quit)


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

# --- Run ----------------------------------------------------------------

print(f"[wipe] starting; HOLD={HOLD_S}s WIPE={WIPE_S}s; ctrl-c to stop")
if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
    die("pipeline failed to enter PLAYING")

GLib.timeout_add(TICK_MS, tick)
try:
    loop.run()
finally:
    pipeline.set_state(Gst.State.NULL)
