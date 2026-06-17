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
    -> queue -> intervideosink channel=chA sync=false

  DECODE PIPELINE B
    identical, channel=chB, file B

  COMPOSITOR PIPELINE (its own Gst.Pipeline; runs forever, never sees
                       any decode-side EOS / flush / segment event)
    intervideosrc channel=chA timeout=200ms do-timestamp=false
    -> videorate skip-to-first=true
    -> video/x-raw,framerate=30/1
    -> queue -> comp.sink_0
    (same for chB -> comp.sink_1)
    compositor name=comp background=black
    -> video/x-raw,width=1280,height=720,framerate=30/1
    -> queue -> videoconvert
    -> video/x-raw,format=NV12,width=1280,height=720
    -> kmssink name=sink sync=true       (HW-scales 1280x720 -> 1360x768
                                          via the vc4 DRM plane)

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
    bin. Two pieces:
      (a) SEGMENT seek with stop=duration (NOT NONE). The previous
          version used Gst.SeekType.NONE for stop; segment completion
          is defined as "streaming reached the segment STOP time", so
          stop=NONE meant SEGMENT_DONE never fired and SEGMENT mode
          also suppresses EOS -> position froze at duration forever.
          Now: query_duration on each demuxer after PAUSED, pass it
          as stop_ns (with hardcoded fallback for asset A=4.75s /
          B=9.08s if the query fails).
      (b) Downstream EVENT pad probe on each qtdemux video src pad
          (install_loop_probe). Catches SEGMENT_DONE OR EOS, DROPs the
          event so it never propagates to v4l2h264dec (no flush -> no
          STREAMOFF -> no bcm2835-codec REQBUFS/EINVAL wedge by
          construction), and schedules the re-seek via GLib.idle_add
          (the seek() call must run on the main thread -- calling it
          inside the streaming-thread probe callback would re-enter
          the streaming chain and deadlock). Bus SEGMENT_DONE messages
          are NOT used for re-arm (would double-fire alongside the
          probe).
  - intervideo bridge isolates each decode pipeline's EOS / flush /
    segment events from the compositor pipeline at the BUFFER level:
    no event propagates across the bridge, so the compositor cannot
    be tipped into EOS or be flushed by a decoder-side restart.
    (Note: operational error-recovery is NOT in this step -- any
    GStreamer ERROR message on any bus still quits the GLib main
    loop and exits the script. A future iteration could NULL the
    affected decode pipeline and keep the compositor running on its
    timeout-fallback frames.)
  - SHARED CLOCK + SHARED BASE-TIME across all three pipelines (forced
    after PAUSED, before any PLAYING). use_clock(SystemClock) + an
    identical set_base_time gives all three a single shared running-
    time origin. do-timestamp=false on each intervideosrc preserves
    the producer PTS instead of re-stamping. (Kept after QA confirmed
    clock alignment alone did NOT change the slow-motion symptom; the
    real bottleneck was CPU videoscale, fixed below. Shared clock
    remains the architecturally-correct setup for three Gst.Pipeline
    objects bridged by intervideo and is harmless to keep.)
  - NO CPU VIDEOSCALE. Source assets are native 1280x720 = the
    composite size; the decode pipelines emit native-size frames
    straight into intervideosink, the compositor pads are sized to
    match, and kmssink HW-scales 1280x720 -> 1360x768 via the vc4
    DRM plane (zero CPU pixel work). QA bisection on glass showed
    `videoscale 1280x720 -> 1360x768` cost ~140ms/frame on a single
    A53 core, pegging the cores and throttling the whole system to
    ~7% real-time. Was present since 674ae5a; was masked by the
    launch/freeze failures.

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
DW, DH = 1280, 720
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
    "videorate", "videoconvert",
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
    where it is canonically supported.

    NO videoscale and NO size capsfilter. Source assets are native
    1280x720 = the chosen composite size (DW x DH); CPU videoscale
    from 1280x720 to a different size cost ~140ms/frame on the Pi
    Zero 2 W ISP-less A53 cores (QA bisection on glass), pegging one
    core per branch and throttling the whole system to ~7% real-time.
    Decoder emits at native 1280x720 in whatever pixel format the
    asset is (I420 for these clips); videorate normalizes the rate to
    30 fps; that goes straight into intervideosink. The compositor
    pads are sized to match (DW x DH) so they do NOT scale either.
    kmssink HW-scales the final NV12 1280x720 to the connector mode
    (1360x768) via the vc4 DRM plane -- zero CPU pixel work."""
    desc = (
        f'filesrc location="{video_path}" name=src '
        f"! qtdemux name=demux ! h264parse "
        f"! v4l2h264dec name=dec "
        f"! videorate ! {RATE_30} "
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
        f"intervideosrc channel=chA timeout=200000000 do-timestamp=false "
        f"  ! videorate skip-to-first=true ! {RATE_30} "
        f"  ! queue max-size-buffers=2 leaky=downstream "
        f"  ! comp.sink_0 "
        f"intervideosrc channel=chB timeout=200000000 do-timestamp=false "
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
#
# Two pieces:
#  - segment_seek(elem, stop_ns): non-flushing SEGMENT|ACCURATE seek
#    from 0 to stop_ns (NOT NONE). stop=NONE was the prior bug -- segment
#    completion is defined as "streaming reached the segment STOP time",
#    so stop=NONE meant SEGMENT_DONE never fired and SEGMENT-mode also
#    suppresses EOS -> position froze at duration with no re-arm signal.
#  - install_loop_probe(demuxer, channel, dur_ns): downstream EVENT pad
#    probe on the demuxer video src pad. Catches SEGMENT_DONE OR EOS;
#    DROPs the event so it never propagates to v4l2h264dec (no flush,
#    no STREAMOFF, no codec wedge -- BY CONSTRUCTION); schedules the
#    re-seek via GLib.idle_add (the seek() call itself must run on the
#    main GLib thread, NOT inside the streaming-thread probe callback,
#    or we get re-entry into the streaming chain).
#
# Why the probe rather than the bus SEGMENT_DONE message: bus messages
# arrive on the main thread but go through async queueing; the probe
# fires the moment the event reaches the qtdemux src pad, so the re-
# seek lands with one-frame-or-better boundary latency. Sink-independent
# and topology-proof.


def segment_seek(elem, stop_ns):
    return elem.seek(
        1.0, Gst.Format.TIME,
        Gst.SeekFlags.SEGMENT | Gst.SeekFlags.ACCURATE,
        Gst.SeekType.SET, 0,
        Gst.SeekType.SET, stop_ns,
    )


def find_video_src_pad(demuxer):
    it = demuxer.iterate_src_pads()
    while True:
        res, pad = it.next()
        if res != Gst.IteratorResult.OK:
            return None
        name = pad.get_name()
        caps = pad.get_current_caps()
        if name.startswith("video") or (
            caps and "video/" in caps.to_string()
        ):
            return pad


def query_dur_ns(pipeline_or_elem, label, fallback_ns):
    ok, dur = pipeline_or_elem.query_duration(Gst.Format.TIME)
    if ok and dur > 0:
        print(f"[wipe_cpu] {label} duration = {dur} ns "
              f"({dur / 1e9:.3f}s)")
        return dur
    print(f"[wipe_cpu] {label} duration query failed; "
          f"using fallback {fallback_ns} ns "
          f"({fallback_ns / 1e9:.3f}s)", file=sys.stderr)
    return fallback_ns


def install_loop_probe(demuxer, channel, dur_ns):
    pad = find_video_src_pad(demuxer)
    if pad is None:
        die(f"[{channel}] no video src pad found on demuxer")

    def _re_seek():
        if not segment_seek(demuxer, dur_ns):
            print(f"[{channel}] idle re-seek FAILED", file=sys.stderr)
        return False  # one-shot via idle_add

    def _on_event(_pad, info):
        event = info.get_event()
        if event is None:
            return Gst.PadProbeReturn.OK
        if event.type in (Gst.EventType.SEGMENT_DONE, Gst.EventType.EOS):
            print(f"[{channel}] {event.type.value_nick} -> idle re-seek")
            GLib.idle_add(_re_seek)
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK

    pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, _on_event)
    print(f"[{channel}] loop probe attached on {pad.get_name()}")


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


def _make_decode_bus_handler(channel):
    """Decode-pipeline bus: ERROR + EOS reporting only. The loop re-arm
    is owned by the EVENT pad probe (install_loop_probe), NOT the bus
    -- bus SEGMENT_DONE messages would double-fire alongside the probe.
    EOS on the bus is unexpected (the probe DROPs EOS at the demuxer
    src) and indicates the probe missed an event; we log + quit so the
    failure is visible rather than silent."""
    def _on_bus(_bus, msg):
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
                  "(probe should have DROPped this!)", file=sys.stderr)
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
    (pipe_a, _make_decode_bus_handler("A")),
    (pipe_b, _make_decode_bus_handler("B")),
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

# Query each demuxer for its file duration (qtdemux has parsed the
# moov box by PAUSED). Fall back to known asset durations if the
# query fails. These durations become the SEGMENT seek stop= boundary.
DUR_A = query_dur_ns(pipe_a, "decodeA", int(4.75 * Gst.SECOND))
DUR_B = query_dur_ns(pipe_b, "decodeB", int(9.08 * Gst.SECOND))

# Install the EVENT pad probe BEFORE the initial seek so the very
# first segment boundary is caught even if it lands surprisingly early.
install_loop_probe(demuxA, "A", DUR_A)
install_loop_probe(demuxB, "B", DUR_B)

# Arm SEGMENT-seek mode on each demuxer BEFORE PLAYING so the very
# first segment runs in segment mode (stop=dur triggers SEGMENT_DONE
# at the end; without stop=dur the previous version froze at end).
for label, demux, dur in [("demuxA", demuxA, DUR_A), ("demuxB", demuxB, DUR_B)]:
    if not segment_seek(demux, dur):
        die(f"initial SEGMENT seek failed on {label}")

# Force one shared clock + one shared base-time on all three pipelines
# BEFORE any of them goes PLAYING. Each Gst.Pipeline normally
# auto-selects its own clock at PLAYING and stamps buffers in its own
# timebase; the intervideo bridge then carries PTS that are only valid
# in the producer pipeline -- pipe_c interprets them against ITS clock,
# kmssink sync=true throttles to match the timebase mismatch, the
# blocking intervideo handoff back-pressures all the way to the
# decoders, and the whole system runs at ~8% real-time (the prior
# slow-motion bug). use_clock pins the clock so the PLAYING transition
# will not re-select; set_base_time after PAUSED with the same value
# everywhere gives all three pipelines a single shared running-time
# origin. do-timestamp=false on intervideosrc (above) preserves the
# producer's PTS instead of re-stamping against the throttled cadence.
shared_clock = Gst.SystemClock.obtain()
shared_base_time = shared_clock.get_time()
for label, p in [("compositor", pipe_c),
                 ("decodeA", pipe_a),
                 ("decodeB", pipe_b)]:
    p.use_clock(shared_clock)
    p.set_base_time(shared_base_time)

# Inline verification log so QA can grep the journal for clock/base-time
# match without needing to add their own probe.
clock_name = shared_clock.get_name() or "?"
print(f"[wipe_cpu] shared clock: {clock_name} "
      f"base_time={shared_base_time} ns")
for label, p in [("compositor", pipe_c),
                 ("decodeA", pipe_a),
                 ("decodeB", pipe_b)]:
    c = p.get_clock()
    bt = p.get_base_time()
    cn = c.get_name() if c is not None else "NONE"
    print(f"[wipe_cpu]   {label}: clock={cn} base_time={bt} ns")

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
