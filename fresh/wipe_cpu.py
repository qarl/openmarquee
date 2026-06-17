#!/usr/bin/env python3
"""fresh/wipe_cpu.py — A <-> B wipe via 3-pipeline intervideo architecture.

Step 2b root-fix (2026-06-17). Built on the per-second [fps] instrument
introduced in fb49201, which localized two structural failures in the
prior single-pipeline approach:

  (P1) Compositor rate-collapse to ~1 fps. compositor's src had no fixed
       framerate; the aggregator ran in deadline-alignment mode against
       two inputs whose running-times diverged (A loops every 4.75s, B
       every 9.08s) under kmssink sync=true backpressure, so it emitted
       roughly one buffer per second to the screen.
  (P2) SEGMENT-seek loop did not re-arm behind the aggregator. Per-source
       SEGMENT_DONE on a multi-pad aggregator is not actionable the same
       way it is for a single-source/single-sink chain; rearmed buffers
       landed behind the compositor's shared running-time and were dropped.

Both are topology problems, not tuning. The fix decouples looping from
compositing via the intervideo bridge:

  DECODE PIPELINE A (its own Gst.Pipeline; single source, single sink)
    filesrc -> qtdemux -> h264parse -> v4l2h264dec
    -> videorate -> video/x-raw,framerate=30/1
    -> videoscale method=1
    -> video/x-raw,format=NV12,width=1360,height=768
    -> queue -> intervideosink channel=chA sync=false

  DECODE PIPELINE B
    identical, channel=chB, file B

  COMPOSITOR PIPELINE (its own Gst.Pipeline; runs forever, never sees
                       any decode-side EOS / flush / segment event)
    intervideosrc channel=chA timeout=200ms
    -> videorate skip-to-first=true
    -> video/x-raw,framerate=30/1
    -> queue -> comp.sink_0
    (same for chB -> comp.sink_1)
    compositor name=comp background=black
    -> video/x-raw,format=NV12-or-AYUV,width=1360,height=768,framerate=30/1
    -> queue -> videoconvert
    -> video/x-raw,format=NV12,width=1360,height=768
    -> kmssink name=sink sync=true

Why this gets the frame flow right:

  - FIXED framerate=30/1 on the compositor pipeline (src cap + per-pad
    videorate) means the aggregator emits at a steady 30 fps regardless
    of input alignment. The 4.75s vs 9.08s loop-duration mismatch
    becomes invisible to the compositor.
  - videorate after each intervideosrc REPEATS the last buffer to
    maintain 30 fps when its decode pipeline briefly stalls (e.g.
    during a SEGMENT_DONE -> re-seek transient). Both compositor pads
    are always fed, so the aggregator never deadline-waits.
  - LOOPING is contained in each single-source / single-sink decode
    bin. Per-decode bus catches SEGMENT_DONE from THAT bin's demuxer
    and re-issues a non-flushing SEGMENT seek to time 0. This IS the
    canonical SEGMENT-seek case that gst supports; the prior failure
    was the aggregator behind it, which is now gone.
  - NO FLUSH event ever reaches v4l2h264dec at the loop boundary
    (SEGMENT seeks are non-flushing). gstreamer's v4l2videodec flush
    handler does NOT call VIDIOC_STREAMOFF/STREAMON on the
    bcm2835-codec CAPTURE queue. The REQBUFS/EINVAL wedge surface
    stays dormant.
  - intervideo bridge isolates each decode pipeline's EOS / flush /
    segment events from the compositor pipeline at the BUFFER level:
    no event propagates across the bridge, so the compositor cannot
    be tipped into EOS or be flushed by a decoder-side restart.
    (Note: operational error-recovery is NOT in this step -- any
    GStreamer ERROR message on any bus still quits the GLib main
    loop and exits the script. A future iteration could NULL the
    affected decode pipeline and keep the compositor running on its
    timeout-fallback frames.)

WIPE (unchanged): GLib timer animates comp.sink_1 xpos/width 0->DW
over WIPE_S; after the wipe roles swap. Both inputs are genuinely
live every frame now (videorate-fed), so motion-through-the-transition
is preserved without any decoder-warm trickery.

INSTRUMENT (preserved + adapted to new topology): per-second [fps]
line continues to print to stderr + /tmp/wipe_fps.log. Probes attach
in the new locations: decA.src in pipe_a, decB.src in pipe_b,
kmssink.sink in pipe_c. Position queries hit pipe_a.demux and
pipe_b.demux. A WORKING result must show screen ~= 30 sustained and
positions cycling 0->dur->0 forever.

NOT in this step (still): the TODO(warm) decoder-warm-up site.
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
DW, DH = 1360, 768
FPS = 30
HOLD_S = 4.0
WIPE_S = 1.0
TICK_MS = 33
PREROLL_BUDGET_S = 30


def die(msg, code=1):
    print(f"[wipe_cpu] {msg}", file=sys.stderr)
    sys.exit(code)


# --- Pre-flights --------------------------------------------------------

for v in VIDEOS:
    if not os.path.exists(v):
        die(f"missing video: {v}")

Gst.init(None)

REQUIRED_ELEMENTS = (
    "filesrc", "qtdemux", "h264parse", "v4l2h264dec",
    "videorate", "videoscale", "videoconvert",
    "intervideosink", "intervideosrc",
    "compositor", "queue", "kmssink",
)
for el in REQUIRED_ELEMENTS:
    if not Gst.ElementFactory.find(el):
        die(f"missing gstreamer element: {el} "
            "(try sudo apt install gstreamer1.0-plugins-{good,bad})")

self_pid = os.getpid()
parent_pid = os.getppid()
own_pids = (str(self_pid), str(parent_pid))
ps = subprocess.run(
    ["pgrep", "-af", "v4l2h264dec|openmarquee-render|mini-play|wipe.py|wipe_cpu"],
    capture_output=True, text=True,
)
peers = [
    line for line in ps.stdout.splitlines()
    if line.split() and line.split()[0] not in own_pids
]
if peers:
    die("another HW-decode process is already running:\n  "
        + "\n  ".join(peers))

# --- Pipelines ----------------------------------------------------------

NV12_DISP = f"video/x-raw,format=NV12,width={DW},height={DH}"
RATE_30 = f"video/x-raw,framerate={FPS}/1"


def build_decode_pipeline(video_path, channel):
    """One source, one sink. SEGMENT-seek loop lives inside this bin
    where it is canonically supported."""
    desc = (
        f'filesrc location="{video_path}" name=src '
        f"! qtdemux name=demux ! h264parse "
        f"! v4l2h264dec name=dec "
        f"! videorate ! {RATE_30} "
        f"! videoscale method=1 ! {NV12_DISP} "
        f"! queue max-size-buffers=4 leaky=downstream "
        f"! intervideosink channel={channel} sync=false"
    )
    try:
        return Gst.parse_launch(desc)
    except GLib.Error as exc:
        die(f"decode pipeline ({channel}) parse failed: {exc.message}")


def build_compositor_pipeline():
    """Runs forever. NEVER sees an EOS or flush from the decoders.
    Per-pad videorate keeps the compositor fed at 30 fps even when an
    upstream intervideosrc momentarily has no buffer."""
    desc = (
        f"compositor name=comp background=black "
        f"  sink_0::xpos=0 sink_0::ypos=0 "
        f"  sink_0::width={DW} sink_0::height={DH} sink_0::zorder=0 "
        f"  sink_1::xpos=0 sink_1::ypos=0 "
        f"  sink_1::width=0 sink_1::height={DH} sink_1::zorder=1 "
        f"! video/x-raw,width={DW},height={DH},framerate={FPS}/1 "
        f"! queue max-size-buffers=2 leaky=downstream "
        f"! videoconvert "
        f"! {NV12_DISP} "
        f"! kmssink name=sink sync=true "
        f"intervideosrc channel=chA timeout=200000000 do-timestamp=true "
        f"  ! videorate skip-to-first=true ! {RATE_30} "
        f"  ! queue max-size-buffers=2 leaky=downstream "
        f"  ! comp.sink_0 "
        f"intervideosrc channel=chB timeout=200000000 do-timestamp=true "
        f"  ! videorate skip-to-first=true ! {RATE_30} "
        f"  ! queue max-size-buffers=2 leaky=downstream "
        f"  ! comp.sink_1"
    )
    try:
        return Gst.parse_launch(desc)
    except GLib.Error as exc:
        die(f"compositor pipeline parse failed: {exc.message}")


pipe_a = build_decode_pipeline(VIDEOS[0], "chA")
pipe_b = build_decode_pipeline(VIDEOS[1], "chB")
pipe_c = build_compositor_pipeline()

decA = pipe_a.get_by_name("dec")
decB = pipe_b.get_by_name("dec")
demuxA = pipe_a.get_by_name("demux")
demuxB = pipe_b.get_by_name("demux")
comp = pipe_c.get_by_name("comp")
sink = pipe_c.get_by_name("sink")
for label, el in [("decA", decA), ("decB", decB),
                  ("demuxA", demuxA), ("demuxB", demuxB),
                  ("comp", comp), ("sink", sink)]:
    if el is None:
        die(f"required element {label} not found after parse_launch")


def find_sink_pad(element, name):
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

# --- Looping (per decode bin) -------------------------------------------


def segment_seek(elem):
    return elem.seek(
        1.0, Gst.Format.TIME,
        Gst.SeekFlags.SEGMENT | Gst.SeekFlags.KEY_UNIT,
        Gst.SeekType.SET, 0,
        Gst.SeekType.NONE, 0,
    )


# --- Frame-flow instrument ---------------------------------------------

counters = {"decA": 0, "decB": 0, "screen": 0}

try:
    fps_log_file = open("/tmp/wipe_fps.log", "a", buffering=1)
    print("[wipe_cpu] tee fps to /tmp/wipe_fps.log")
except OSError as _exc:
    fps_log_file = None
    print(f"[wipe_cpu] /tmp/wipe_fps.log unavailable ({_exc}); stderr only")


def _make_counter_probe(key):
    def _probe(_pad, _info):
        counters[key] += 1
        return Gst.PadProbeReturn.OK
    return _probe


def attach_flow_probes():
    for label, element, padname in [
        ("decA", decA, "src"),
        ("decB", decB, "src"),
        ("screen", sink, "sink"),
    ]:
        pad = element.get_static_pad(padname)
        if pad is None:
            die(f"{label}: element has no {padname} pad")
        pad.add_probe(Gst.PadProbeType.BUFFER, _make_counter_probe(label))


def _query_pos_s(elem):
    ok, pos_ns = elem.query_position(Gst.Format.TIME)
    if not ok or pos_ns < 0:
        return "?"
    return f"{pos_ns / 1e9:.2f}"


_t_start = time.monotonic()


def fps_tick():
    uptime = int(time.monotonic() - _t_start)
    posA = _query_pos_s(demuxA)
    posB = _query_pos_s(demuxB)
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
    return True


# --- State machine ------------------------------------------------------

outgoing = 0
incoming = 1
state = "HOLD"
phase_start_ns = 0


def now_ns():
    return GLib.get_monotonic_time() * 1000


def set_geom(pad, x, w):
    pad.set_property("xpos", int(x))
    pad.set_property("width", int(w))


def start_wipe():
    global state, phase_start_ns
    # TODO(warm): with intervideosrc + videorate the incoming pad is
    # already being driven at 30 fps from its decoder, so warming should
    # be implicit. If glass shows a stutter at the leading edge of the
    # reveal, this is the site: temporarily zorder=1 + width=1 on the
    # incoming pad for ~150-300ms before animation start.
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
    return True


set_geom(pads[0], 0, DW)
set_geom(pads[1], 0, 0)
phase_start_ns = now_ns()

# --- Bus + signals ------------------------------------------------------

loop = GLib.MainLoop()


def _make_decode_bus_handler(channel, demux_obj):
    def _on_bus(_bus, msg):
        if msg.type == Gst.MessageType.SEGMENT_DONE:
            src = msg.src
            if src is demux_obj:
                if not segment_seek(src):
                    print(f"[{channel}] SEGMENT_DONE re-seek FAILED",
                          file=sys.stderr)
            return
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            src = msg.src.get_name() if msg.src else "?"
            print(f"[{channel}] ERROR {src}: {err.message}",
                  file=sys.stderr)
            if dbg:
                print(f"[{channel}]  debug: {dbg}", file=sys.stderr)
            loop.quit()
            return
        if msg.type == Gst.MessageType.EOS:
            print(f"[{channel}] pipeline EOS reached bus "
                  "(segment mode dropped?)", file=sys.stderr)
            loop.quit()
    return _on_bus


def on_comp_bus(_bus, msg):
    if msg.type == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        src = msg.src.get_name() if msg.src else "?"
        print(f"[comp] ERROR {src}: {err.message}", file=sys.stderr)
        if dbg:
            print(f"[comp]  debug: {dbg}", file=sys.stderr)
        loop.quit()
        return
    if msg.type == Gst.MessageType.EOS:
        print("[comp] pipeline EOS (decoder-isolated bridge should "
              "have prevented this)", file=sys.stderr)
        loop.quit()


for p, h in [
    (pipe_a, _make_decode_bus_handler("A", demuxA)),
    (pipe_b, _make_decode_bus_handler("B", demuxB)),
    (pipe_c, on_comp_bus),
]:
    bus = p.get_bus()
    bus.add_signal_watch()
    bus.connect("message", h)


shutdown_requested = False


def shutdown(*_):
    global shutdown_requested
    shutdown_requested = True
    GLib.idle_add(loop.quit)


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

# --- Run ----------------------------------------------------------------


def preroll_to_paused(p, label):
    """Set p to PAUSED and poll get_state up to PREROLL_BUDGET_S
    seconds. Logs each ASYNC tick. Honors shutdown_requested.

    Live pipelines (compositor pipe_c contains intervideosrc x2) reach
    PAUSED but return NO_PREROLL rather than SUCCESS -- live sources
    only produce buffers in PLAYING. NO_PREROLL is a "yes, in PAUSED,
    will produce on PLAYING" answer, NOT a failure; accept it as
    success-equivalent so the live compositor pipeline does not
    30-second-budget-out waiting for a SUCCESS that will never arrive.
    File-backed decode pipelines still return SUCCESS via the ASYNC
    path (their non-live sources DO preroll a buffer in PAUSED, which
    is what the initial SEGMENT seek then arms)."""
    ret = p.set_state(Gst.State.PAUSED)
    if ret == Gst.StateChangeReturn.FAILURE:
        die(f"{label}: pipeline failed to enter PAUSED")
    if ret == Gst.StateChangeReturn.NO_PREROLL:
        print(f"[wipe_cpu] {label} reached PAUSED (NO_PREROLL "
              "-- live source, will produce in PLAYING)")
        return True
    for elapsed in range(1, PREROLL_BUDGET_S + 1):
        if shutdown_requested:
            return False
        ret, cur, pending = p.get_state(1 * Gst.SECOND)
        if ret == Gst.StateChangeReturn.SUCCESS:
            print(f"[wipe_cpu] {label} preroll done after ~{elapsed}s "
                  f"(state={cur.value_nick})")
            return True
        if ret == Gst.StateChangeReturn.NO_PREROLL:
            print(f"[wipe_cpu] {label} reached PAUSED after ~{elapsed}s "
                  "(NO_PREROLL -- live source)")
            return True
        if ret == Gst.StateChangeReturn.FAILURE:
            die(f"{label} preroll FAILURE")
        print(f"[wipe_cpu] {label} preroll... state={cur.value_nick} "
              f"pending={pending.value_nick} "
              f"({elapsed}/{PREROLL_BUDGET_S}s)")
    die(f"{label} preroll exceeded {PREROLL_BUDGET_S}s budget")


print(f"[wipe_cpu] starting; HOLD={HOLD_S}s WIPE={WIPE_S}s "
      f"display={DW}x{DH}@{FPS} intervideo-bridged; ctrl-c to stop")

# Start the compositor pipeline first so the intervideosrc endpoints on
# chA / chB exist before the decode pipelines start emitting buffers.
preroll_to_paused(pipe_c, "compositor")
preroll_to_paused(pipe_a, "decodeA")
preroll_to_paused(pipe_b, "decodeB")

if shutdown_requested:
    for p in (pipe_a, pipe_b, pipe_c):
        p.set_state(Gst.State.NULL)
    sys.exit(0)

# Arm SEGMENT-seek mode on each demuxer BEFORE PLAYING so the very
# first segment runs in segment mode (SEGMENT_DONE rather than EOS).
for label, demux in [("demuxA", demuxA), ("demuxB", demuxB)]:
    if not segment_seek(demux):
        die(f"initial SEGMENT seek failed on {label}")

for label, p in [("compositor", pipe_c),
                 ("decodeA", pipe_a),
                 ("decodeB", pipe_b)]:
    if p.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        die(f"{label}: pipeline failed to enter PLAYING")

attach_flow_probes()
GLib.timeout_add(TICK_MS, tick)
GLib.timeout_add(1000, fps_tick)
try:
    loop.run()
finally:
    for p in (pipe_a, pipe_b, pipe_c):
        p.set_state(Gst.State.NULL)
    if fps_log_file is not None:
        try:
            fps_log_file.close()
        except OSError:
            pass
