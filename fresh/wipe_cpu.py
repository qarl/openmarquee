#!/usr/bin/env python3
"""fresh/wipe_cpu.py — A <-> B wipe, CPU compositor at native display res.

Step 2b pivot (2026-06-17). Sibling to fresh/wipe.py; that file stays
as the GL reference. THIS file is the service-runnable version.

Iteration on top of 674ae5a: add-only frame-flow instrument (BUFFER
probes on decA src, decB src, kmssink sink; once-per-second [fps] log
line with counters + per-source stream position + state). No pipeline,
loop, or wipe logic touched — instrument-only diagnostic to localize
a frozen-frame-with-no-errors symptom QA cannot see on the glass.

Why pivot from GL: the GL version (wipe.py @ e493068) renders smooth on
glass but will not preroll as a headless systemd service. GST_GL_WINDOW=
gbm hangs at READY->PAUSED forever (GL context init blocks waiting for a
session that does not exist); EGL surfaceless fails fast with "Failed to
create EGLDisplay from native display." The only remaining GL fix is a
hand-built GstGLDisplay on the render node /dev/dri/renderD128 — code we
can't iterate on locally and that wins nothing when the panel runs at
1360x768 anyway (~2x wasted blend work at 1080p). qarl's call: prefer
the simple robust path.

Pipeline (CPU, sized to native, one explicit videoconvert at output):

  filesrc -> qtdemux -> h264parse -> v4l2h264dec (HW)
          -> videoscale method=1 (bilinear)
          -> video/x-raw,width=1360,height=768  (size only; format
                                                 left to negotiation)
          -> comp.sink_0 / comp.sink_1

  compositor name=comp  (CPU blend in whatever format it prefers,
                         typically AYUV / ARGB internally)
          -> videoconvert
          -> video/x-raw,format=NV12,width=1360,height=768
          -> kmssink sync=true                  (native modeset, NV12)

Perf reasoning + honesty:
- Subagent caught the v3 docstring claim of "zero videoconvert" as
  wrong: mainline compositor does NOT blend NV12 natively, it
  negotiates to AYUV/ARGB internally and gstreamer would silently
  inject a videoconvert per stream upstream. Letting upstream caps
  negotiate (no format=NV12 on input) means at MOST one input
  format conversion per stream, picked by compositor itself — which
  is the minimum achievable without GL.
- One explicit videoconvert sits between compositor and kmssink so
  the output reaches NV12 for kmssink (vc4 KMS plane native format),
  no implicit per-frame conversion path inside kmssink.
- videoscale downscales 1920x1080 -> 1360x768 (~0.50x area) BEFORE
  the blend so compositor blends ~1 MP per stream not ~2 MP. This
  is the leverage QA called out for moving CPU off the cliff.
- kmssink takes the NV12 1360x768 output and KMS-atomic-commits at
  the panel's native mode — no output scale.

Kept from e493068 (verified on glass already):
- non-flushing SEGMENT-seek loop: SEGMENT_DONE on the bus -> re-arm
  with seek(rate=1.0, TIME, SEGMENT|KEY_UNIT, SET 0, NONE 0). No FLUSH
  event reaches v4l2h264dec — no STREAMOFF/STREAMON, no REQBUFS/EINVAL
  surface.
- wipe animation: sink_1 xpos=0 width animated 0->DW over WIPE_S; after
  wipe, roles swap.
- 30s preroll polling loop with progress logging — overkill for the CPU
  path (no GL, preroll should be <1s) but harmless and protects against
  future slow-init sources.
- SIGTERM-clean shutdown: shutdown_requested flag checked in preroll;
  GLib.idle_add(loop.quit) post-PLAYING.
- Two-HW-decoder enumeration sanity, peer-process double-start guard.

Dropped from e493068 (no longer needed without GL):
- GST_GL_PLATFORM / GST_GL_WINDOW / GST_GL_API env defaults.
- _ensure_xdg_runtime_dir() helper.
- glupload / glcolorconvert / glvideomixer / gldownload from the
  required-element list.

TODO(warm) decoder-warmup mitigation site is still NOT this step.
"""

import os
import signal
import subprocess
import sys
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gst  # noqa: E402

VIDEOS = [
    "/var/openmarquee/content/029c4d68-744c-4d30-9adc-0f37c55514f1/asset.mp4",
    "/var/openmarquee/content/3f54a4d2-a120-4c0c-aa80-5b99aaf7c9ff/asset.mp4",
]
DW, DH = 1360, 768  # native display res; matches kmssink mode + blend res
HOLD_S = 4.0
WIPE_S = 1.0
TICK_MS = 33


def die(msg, code=1):
    print(f"[wipe_cpu] {msg}", file=sys.stderr)
    sys.exit(code)


# --- Pre-flights --------------------------------------------------------

for v in VIDEOS:
    if not os.path.exists(v):
        die(f"missing video: {v}")

Gst.init(None)

for el in (
    "filesrc", "qtdemux", "h264parse", "v4l2h264dec",
    "videoscale", "compositor", "videoconvert", "kmssink",
):
    if not Gst.ElementFactory.find(el):
        die(f"missing gstreamer element: {el} "
            "(try sudo apt install gstreamer1.0-plugins-{good,bad})")

# Single HW decoder context — refuse double-start. Exclude both this
# process AND its parent (the shell / systemd-run scope wrapper);
# those carry the script's argv in their cmdline and would otherwise
# false-positive.
self_pid = os.getpid()
parent_pid = os.getppid()
own = (str(self_pid), str(parent_pid))
ps = subprocess.run(
    ["pgrep", "-af", "v4l2h264dec|openmarquee-render|mini-play|wipe.py|wipe_cpu"],
    capture_output=True, text=True,
)
peers = [
    line for line in ps.stdout.splitlines()
    if line.split() and line.split()[0] not in own
]
if peers:
    die("another HW-decode process is already running:\n  "
        + "\n  ".join(peers))

# --- Pipeline -----------------------------------------------------------

# Input caps: size only (let compositor pick blend format).
# Output caps: NV12 at native for kmssink, with an explicit videoconvert
# so the AYUV/ARGB compositor output is converted ONCE at the seam.
INPUT_SIZE = f"video/x-raw,width={DW},height={DH}"
OUTPUT_NV12 = f"video/x-raw,format=NV12,width={DW},height={DH}"

pipe_desc = (
    f"compositor name=comp background=black "
    f"  sink_0::xpos=0 sink_0::ypos=0 "
    f"  sink_0::width={DW} sink_0::height={DH} sink_0::zorder=0 "
    f"  sink_1::xpos=0 sink_1::ypos=0 "
    f"  sink_1::width=0 sink_1::height={DH} sink_1::zorder=1 "
    f"! videoconvert ! {OUTPUT_NV12} ! kmssink name=sink sync=true "
    f'filesrc location="{VIDEOS[0]}" name=srcA '
    f"  ! qtdemux name=demuxA ! h264parse ! v4l2h264dec name=decA "
    f"  ! videoscale method=1 ! {INPUT_SIZE} ! comp.sink_0 "
    f'filesrc location="{VIDEOS[1]}" name=srcB '
    f"  ! qtdemux name=demuxB ! h264parse ! v4l2h264dec name=decB "
    f"  ! videoscale method=1 ! {INPUT_SIZE} ! comp.sink_1"
)

try:
    pipeline = Gst.parse_launch(pipe_desc)
except GLib.Error as exc:
    die(f"pipeline parse failed: {exc.message}")

comp = pipeline.get_by_name("comp")
if comp is None:
    die("compositor not found in parsed pipeline")


def find_sink_pad(element, name):
    """compositor pads are request pads; get_static_pad behavior on
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


pads = [find_sink_pad(comp, "sink_0"), find_sink_pad(comp, "sink_1")]
if pads[0] is None or pads[1] is None:
    die(f"could not locate compositor pads sink_0/sink_1: {pads}")

# --- Per-source SEGMENT-seek loop ---------------------------------------

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


# --- Frame-flow instrument (per-second [fps] line) ----------------------
#
# Add-only diagnostic per QA dispatch 2026-06-17. The state machine
# log "wipe/hold" prints regardless of whether frames are flowing;
# decoder-open != frame-delivering; the pipeline emits no error when
# a decoder silently stops producing. Pad probes here give a true
# frame-flow signal at three points:
#
#   - decA src pad -> frames OUT of HW decoder A
#   - decB src pad -> frames OUT of HW decoder B
#   - kmssink sink pad -> frames actually reaching the screen
#
# Once per second a single line prints uptime, all three counters
# (reset after each tick), each demuxer's stream position, and the
# current state. Reading the line:
#   decA/decB drop to 0 -> THAT decoder stalled
#   decA/decB > 0 but screen = 0 -> stall is downstream of decoders
#   posA/posB frozen with fps > 0 -> seek/segment confusion
#   line stops appearing -> process or main loop hung

counters = {"decA": 0, "decB": 0, "screen": 0}

try:
    fps_log_file = open("/tmp/wipe_fps.log", "a", buffering=1)
    print(f"[wipe_cpu] tee fps to /tmp/wipe_fps.log")
except OSError as _exc:
    fps_log_file = None
    print(f"[wipe_cpu] /tmp/wipe_fps.log unavailable ({_exc}); stderr only")


def _make_counter_probe(key):
    def _probe(_pad, _info):
        counters[key] += 1
        return Gst.PadProbeReturn.OK
    return _probe


def attach_flow_probes():
    for name in ("decA", "decB"):
        el = pipeline.get_by_name(name)
        if el is None:
            die(f"{name} element not found for flow probe")
        pad = el.get_static_pad("src")
        if pad is None:
            die(f"{name} has no src pad")
        pad.add_probe(Gst.PadProbeType.BUFFER, _make_counter_probe(name))
    sink_el = pipeline.get_by_name("sink")
    if sink_el is None:
        die("kmssink element not found for flow probe")
    sink_pad = sink_el.get_static_pad("sink")
    if sink_pad is None:
        die("kmssink has no sink pad")
    sink_pad.add_probe(
        Gst.PadProbeType.BUFFER, _make_counter_probe("screen")
    )


def _query_pos_s(elem):
    ok, pos_ns = elem.query_position(Gst.Format.TIME)
    if not ok or pos_ns < 0:
        return "?"
    return f"{pos_ns / 1e9:.2f}"


_t_start = time.monotonic()


def fps_tick():
    uptime = int(time.monotonic() - _t_start)
    posA = _query_pos_s(demuxes["demuxA"])
    posB = _query_pos_s(demuxes["demuxB"])
    line = (
        f"[fps] t={uptime} "
        f"decA={counters['decA']} decB={counters['decB']} "
        f"screen={counters['screen']} "
        f"posA={posA} posB={posB} state={state}"
    )
    print(line, file=sys.stderr, flush=True)
    if fps_log_file is not None:
        try:
            fps_log_file.write(line + "\n")
        except OSError:
            pass
    counters["decA"] = 0
    counters["decB"] = 0
    counters["screen"] = 0
    return True  # keep firing


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
print(f"[wipe_cpu] HW decoders in pipeline: {len(hw_decs)} -> {hw_decs}")
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
    # SEGMENT-seek loop both decoders stay in PLAYING continuously, so
    # warming should already be implicit -- but if the wipe seam stutters
    # on glass, the knob site is: temporarily zorder=1 + width=1 on the
    # incoming pad for ~200ms before animation start (single-pixel reveal
    # warm-up). Still NOT this step.
    pads[incoming].set_property("zorder", 1)
    pads[outgoing].set_property("zorder", 0)
    state = "WIPE"
    phase_start_ns = now_ns()
    print(f"[wipe_cpu] wipe {outgoing} -> {incoming} begin")


def finish_wipe():
    global state, phase_start_ns, outgoing, incoming
    set_geom(pads[incoming], 0, DW)
    set_geom(pads[outgoing], 0, 0)
    outgoing, incoming = incoming, outgoing
    state = "HOLD"
    phase_start_ns = now_ns()
    print(f"[wipe_cpu] hold {outgoing}")


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
            set_geom(pads[incoming], 0, (elapsed_s / WIPE_S) * DW)
    return True  # keep firing


set_geom(pads[0], 0, DW)
set_geom(pads[1], 0, 0)
phase_start_ns = now_ns()

# --- Bus + signals ------------------------------------------------------

loop = GLib.MainLoop()


def on_bus(_bus, msg):
    if msg.type == Gst.MessageType.SEGMENT_DONE:
        src = msg.src
        name = src.get_name() if src else "?"
        if name in DEMUX_NAMES:
            if not segment_seek(src):
                print(f"[wipe_cpu] {name} segment re-seek FAILED",
                      file=sys.stderr)
        return
    if msg.type == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        src = msg.src.get_name() if msg.src else "?"
        print(f"[wipe_cpu] ERROR {src}: {err.message}", file=sys.stderr)
        if dbg:
            print(f"[wipe_cpu]  debug: {dbg}", file=sys.stderr)
        loop.quit()
        return
    if msg.type == Gst.MessageType.EOS:
        print("[wipe_cpu] pipeline EOS reached bus (segment mode dropped?)",
              file=sys.stderr)
        loop.quit()


bus = pipeline.get_bus()
bus.add_signal_watch()
bus.connect("message", on_bus)


shutdown_requested = False


def shutdown(*_):
    global shutdown_requested
    shutdown_requested = True
    GLib.idle_add(loop.quit)


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

# --- Run ----------------------------------------------------------------

print(f"[wipe_cpu] starting; HOLD={HOLD_S}s WIPE={WIPE_S}s "
      f"display={DW}x{DH} NV12; ctrl-c to stop")

if pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
    die("pipeline failed to enter PAUSED")

PREROLL_BUDGET_S = 30
POLL_S = 1
prerolled = False
for elapsed in range(POLL_S, PREROLL_BUDGET_S + POLL_S, POLL_S):
    if shutdown_requested:
        pipeline.set_state(Gst.State.NULL)
        sys.exit(0)
    ret, cur, pending = pipeline.get_state(POLL_S * Gst.SECOND)
    if ret == Gst.StateChangeReturn.SUCCESS:
        print(f"[wipe_cpu] preroll done after ~{elapsed}s "
              f"(state={cur.value_nick})")
        prerolled = True
        break
    if ret == Gst.StateChangeReturn.FAILURE:
        die("pipeline preroll FAILURE")
    print(f"[wipe_cpu] preroll... state={cur.value_nick} "
          f"pending={pending.value_nick} ({elapsed}/{PREROLL_BUDGET_S}s)")

if not prerolled:
    die(f"pipeline preroll exceeded {PREROLL_BUDGET_S}s budget")

for name, demux in demuxes.items():
    if not segment_seek(demux):
        die(f"initial SEGMENT seek failed on {name}")

if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
    die("pipeline failed to enter PLAYING")

attach_flow_probes()

GLib.timeout_add(TICK_MS, tick)
GLib.timeout_add(1000, fps_tick)
try:
    loop.run()
finally:
    pipeline.set_state(Gst.State.NULL)
    if fps_log_file is not None:
        try:
            fps_log_file.close()
        except OSError:
            pass
