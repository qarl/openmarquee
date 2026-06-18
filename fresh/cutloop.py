#!/usr/bin/env python3
"""fresh/cutloop.py -- one long-lived v4l2h264dec, gapless looping
playlist via gstreamer concat with dynamic source addition.

Per qarl + QA pivot from wipe_cpu.py: tear out the wipe / preload /
scheduler / gap-killer / watchdog machinery; start fresh. Single
decoder, seamless back-to-back cuts, zero boundary stall.

QA pre-test verified (on glass): A (4.75s) + B (9.08s) decoded
through ONE long-lived v4l2h264dec via concat in 14.9s wall =
real-time, zero errors, no STREAMOFF at the boundary (concat absorbs
the intermediate EOS; the decoder handled B's different SPS/PPS
without a re-init stall). The seamless single-decoder approach
WORKS.

Pipeline (built programmatically, NOT parse_launch, so we can add
sources dynamically):

  filesrc(A) ! qtdemux ! h264parse config-interval=-1 ! concat.sink_0
  filesrc(B) ! qtdemux ! h264parse config-interval=-1 ! concat.sink_1
  filesrc(A) ! ... ! concat.sink_2     (initial queue depth = 4)
  filesrc(B) ! ... ! concat.sink_3
  concat name=c
    ! v4l2h264dec
    ! videoconvert ! video/x-raw,format=NV12,width=1280,height=720
    ! kmssink sync=true

LOOP MECHANISM (the seamless one): per QA, a naive EOS -> seek-to-0
WILL gap because the seek event causes STREAMOFF. Instead this
script DYNAMICALLY ADDS new source bins to concat on each source's
EOS, so the concat playlist never runs dry. concat absorbs the
intra-list EOS without emitting EOS downstream and without
STREAMOFFing the decoder. The loop boundary is just another
source-to-source switch within concat -- identical mechanism to
clip-to-clip within the playlist (which QA proved seamless).

  - Each sub-bin (filesrc -> qtdemux -> h264parse) has a downstream
    EVENT probe on its ghost src pad. When EOS flows through, the
    probe schedules add_next_clip via GLib.idle_add (main thread).
  - playlist_added_count cycles through VIDEOS so additions
    preserve playlist order (A, B, A, B, ...).
  - INITIAL_QUEUE_DEPTH = 4 (two full cycles) gives margin against
    idle_add scheduling lag between source EOS and the new bin
    being linked + state-synced + ready.

h264parse config-interval=-1: re-inject SPS/PPS at every IDR so the
decoder has a fresh parameter set at every source boundary. Per QA
pre-test the bcm2835-codec accepts B's different SPS/PPS without
re-init when the parameter sets arrive in-band with each IDR.

INSTRUMENT (minimal per qarl): BUFFER probe on kmssink.sink counts
frames + tracks max inter-buffer gap. 1Hz [fps] line to stderr +
/tmp/wipe_fps.log (truncated on open, run-start marker). Warns on
any gap > 50ms -- the boundary stall qarl wants verified absent on
data.

NOT included (deliberately, per qarl "tear it all out"):
  no preload, no wipe, no scheduler, no gap-killer, no watchdog, no
  per-clip teardown, no dual-decoder overlap, no compositor, no
  intervideo bridge, no segment-seek loop.
"""

import gc
import os
import signal
import subprocess
import sys
import time
import weakref

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gst  # noqa: E402

VIDEOS = [
    "/var/openmarquee/content/029c4d68-744c-4d30-9adc-0f37c55514f1/asset.mp4",
    "/var/openmarquee/content/3f54a4d2-a120-4c0c-aa80-5b99aaf7c9ff/asset.mp4",
]
DW, DH = 1280, 720
GAP_WARN_MS = 50
# Per QA QUEUE-AHEAD INVARIANT: ALWAYS keep >=1 pad pending ahead of
# current. Pre-queue both playlist clips at startup; on each EOS the
# probe eagerly schedules add_next_clip so the queue stays at >=1
# pending at all times. Steady state: 2 pads alive (one active + one
# pending), with retire_subgraph removing the just-EOSed sub-bin on
# the next main-thread idle.
INITIAL_QUEUE_DEPTH = 2


def die(msg, code=1):
    print(f"[cutloop] {msg}", file=sys.stderr)
    sys.exit(code)


# --- Pre-flights --------------------------------------------------------

for v in VIDEOS:
    if not os.path.exists(v):
        die(f"missing video: {v}")

Gst.init(None)

REQUIRED_ELEMENTS = (
    "filesrc", "qtdemux", "h264parse", "v4l2h264dec",
    "queue", "concat", "kmssink",
)
for el in REQUIRED_ELEMENTS:
    if not Gst.ElementFactory.find(el):
        die(f"missing gstreamer element: {el} "
            "(try sudo apt install gstreamer1.0-plugins-{good,bad})")

self_pid = os.getpid()
parent_pid = os.getppid()
own_pids = (str(self_pid), str(parent_pid))
ps = subprocess.run(
    ["pgrep", "-af",
     "v4l2h264dec|openmarquee-render|mini-play|wipe.py|wipe_cpu|cutloop"],
    capture_output=True, text=True,
)
peers = [
    line for line in ps.stdout.splitlines()
    if line.split() and line.split()[0] not in own_pids
]
if peers:
    die("another HW-decode process is already running:\n  "
        + "\n  ".join(peers))


# GLib main loop defined early so add_next_clip's defensive
# loop.quit() path is callable from the INITIAL add loop (before
# the main loop has even started running) as well as from runtime
# EOS-triggered adds. loop.quit() pre-run is a no-op; post-run it
# triggers the finally cleanup.
loop = GLib.MainLoop()


# --- Pipeline (built programmatically for dynamic source add) ----------

pipeline = Gst.Pipeline.new("cutloop")
if pipeline is None:
    die("Gst.Pipeline.new returned None")

# Pipeline: concat -> v4l2h264dec -> queue -> kmssink.
#
# Drop the format-only capsfilter that was here in cf03a8f -- per
# QA isolation: a "video/x-raw,format=NV12" capsfilter BEFORE
# kmssink fails preroll because v4l2h264dec src caps are not fixed
# until it decodes the first frame via V4L2 SOURCE_CHANGE. The
# capsfilter blocked the PAUSED transition with no first frame
# yet to negotiate against. QA tested the naked concat ! decoder
# ! kmssink directly on glass and it prerolled + ran clean.
#
# Keep a plain queue (no caps) between decoder and kmssink for
# jitter absorption -- QA explicitly OK'd this.
concat = Gst.ElementFactory.make("concat", "c")
decoder = Gst.ElementFactory.make("v4l2h264dec", "dec")
out_queue = Gst.ElementFactory.make("queue", "outq")
sink = Gst.ElementFactory.make("kmssink", "sink")
sink.set_property("sync", True)

for el in (concat, decoder, out_queue, sink):
    if el is None:
        die("factory.make returned None for a core element")
    pipeline.add(el)

if not concat.link(decoder):
    die("link concat -> decoder failed")
if not decoder.link(out_queue):
    die("link decoder -> queue failed")
if not out_queue.link(sink):
    die("link queue -> kmssink failed")


# --- Dynamic source addition (the loop mechanism) ----------------------

playlist_added_count = [0]  # cumulative adds (cycles through VIDEOS)
live_subgraph_count = [0]   # current live sub-bin count (incremented
                            # in add_next_clip, decremented in
                            # retire_subgraph). Per QA: this is the
                            # value that should hover at ~2, NOT the
                            # cumulative playlist_added_count which
                            # grows unbounded.


def add_next_clip():
    """Append the next playlist clip (cyclic order) to concat as a
    new sub-bin. Called from main thread (initial loop + GLib.
    idle_add from EOS probe on prior sub-bin)."""
    clip_idx = playlist_added_count[0] % len(VIDEOS)
    asset = VIDEOS[clip_idx]
    serial = playlist_added_count[0]
    playlist_added_count[0] += 1
    asset_name = os.path.basename(os.path.dirname(asset)) or asset

    sub = Gst.Bin.new(f"src_bin_{serial}")
    filesrc = Gst.ElementFactory.make("filesrc", None)
    filesrc.set_property("location", asset)
    qtdemux = Gst.ElementFactory.make("qtdemux", None)
    h264parse = Gst.ElementFactory.make("h264parse", None)
    h264parse.set_property("config-interval", -1)
    for el in (filesrc, qtdemux, h264parse):
        if el is None:
            print(f"[cutloop] [bin {serial}] factory.make failed -- "
                  "quitting", file=sys.stderr)
            loop.quit()
            return False
        sub.add(el)
    if not filesrc.link(qtdemux):
        print(f"[cutloop] [bin {serial}] filesrc -> qtdemux link "
              "failed -- quitting", file=sys.stderr)
        loop.quit()
        return False

    # qtdemux has a dynamic video pad; link on pad-added. Use
    # caps.to_string() (binding-portable; get_structure().get_name()
    # raises StructureWrapper.get_name AttributeError on this
    # PyGObject build per a prior dispatch).
    def _on_pad_added(_demux, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        caps_str = caps.to_string() if caps else ""
        if not caps_str.startswith("video/"):
            return
        sink_pad = h264parse.get_static_pad("sink")
        if sink_pad is None or sink_pad.is_linked():
            return
        pad.link(sink_pad)
    # STORE handler id so retire_subgraph can disconnect later.
    # Per QA leak analysis: without this, the closure on _on_pad_added
    # keeps h264parse alive -> keeps the sub-bin alive -> leak.
    pad_added_id = qtdemux.connect("pad-added", _on_pad_added)

    # Ghost the h264parse src pad up through the sub-bin so we can
    # link it into a concat request pad.
    h264_src = h264parse.get_static_pad("src")
    ghost = Gst.GhostPad.new("src", h264_src)
    sub.add_pad(ghost)

    pipeline.add(sub)
    # request_pad_simple is the gst 1.20+ replacement for the
    # deprecated get_request_pad. Fall back so we work on older
    # builds too.
    if hasattr(concat, "request_pad_simple"):
        concat_sink = concat.request_pad_simple("sink_%u")
    else:
        concat_sink = concat.get_request_pad("sink_%u")
    if concat_sink is None:
        print(f"[cutloop] [bin {serial}] concat sink request "
              "failed -- quitting", file=sys.stderr)
        loop.quit()
        return False
    if ghost.link(concat_sink) != Gst.PadLinkReturn.OK:
        print(f"[cutloop] [bin {serial}] ghost -> concat.sink link "
              "failed -- quitting", file=sys.stderr)
        loop.quit()
        return False

    # EOS probe lives on the CONCAT SINK pad (per QA dispatch), not
    # the sub-bin ghost. When concat sees EOS on this sink pad, it
    # switches away to the next pending sink pad and ABSORBS the
    # EOS internally -- no downstream EOS, no STREAMOFF on the
    # decoder. The probe schedules two main-thread ops via
    # idle_add: (a) RETIRE this subgraph (NULL state, remove from
    # pipeline, release concat request pad, AND disconnect the
    # qtdemux pad-added handler + remove this probe so the
    # closures release the sub-bin), and (b) ADD the next playlist
    # clip to keep the queue-ahead invariant.
    eos_probe_id = None  # filled in below

    def _on_concat_sink_event(_pad, info):
        ev = info.get_event()
        if ev and ev.type == Gst.EventType.EOS:
            print(f"[cutloop] concat sink EOS from bin {serial} "
                  f"({asset_name}) -> retire + queue next",
                  file=sys.stderr)
            # Pass IDs needed for clean retire (disconnect signal +
            # remove probe so closures release the sub-bin). Per QA
            # leak analysis: without these, set_state(NULL) +
            # pipeline.remove + release_request_pad alone leaks.
            GLib.idle_add(retire_subgraph, sub, concat_sink,
                          qtdemux, pad_added_id, eos_probe_id)
            GLib.idle_add(add_next_clip)
        return Gst.PadProbeReturn.OK
    eos_probe_id = concat_sink.add_probe(
        Gst.PadProbeType.EVENT_DOWNSTREAM, _on_concat_sink_event
    )

    sub.sync_state_with_parent()
    live_subgraph_count[0] += 1
    # Stamp the boundary timestamp so subsequent GAP-WARNs within
    # BOUNDARY_PROXIMITY_MS are tagged BOUNDARY (i.e. the sub-bin
    # build spike) rather than INTERIOR (steady pacing).
    last_boundary_ns[0] = time.monotonic_ns()
    print(f"[cutloop] queued bin {serial} = {asset_name} "
          f"(live_bins={live_subgraph_count[0]} "
          f"playlist_added={playlist_added_count[0]})",
          file=sys.stderr)
    return False  # one-shot when called via GLib.idle_add


def retire_subgraph(sub_bin, concat_sink_pad, qtdemux_elem,
                    pad_added_id, eos_probe_id):
    """Tear down a sub-bin after its EOS has passed through concat.
    Per QA leak analysis: set_state(NULL) + pipeline.remove +
    release_request_pad ALONE LEAKS because Python closures captured
    by the qtdemux pad-added handler AND the concat-sink EOS probe
    still hold refs to the sub-bin's elements. Without explicit
    disconnect + remove_probe, the sub-bin is never garbage-collected
    -- queued_clips grows unbounded, threads + heap thrash a single
    core, throttling kmssink to ~7fps over a 180s soak (QA observed
    4bf2b05). The retire-leak IS the throttle; fixing it should
    restore 30fps without other changes.

    Sequence per QA priority:
      1. qtdemux_elem.disconnect(pad_added_id) -- releases the
         pad-added closure which captures h264parse + sub_bin.
      2. concat_sink_pad.remove_probe(eos_probe_id) -- releases
         the EOS-probe closure which captures sub + concat_sink.
      3. sub_bin.set_state(NULL) -- releases filesrc fd, qtdemux
         parsed state, h264parse.
      4. pipeline.remove(sub_bin) -- detaches from parent.
      5. concat.release_request_pad(concat_sink_pad).
      6. Decrement live_subgraph_count.
      7. weakref.ref + GLib.idle_add(gc.collect + check_alive) for
         leak evidence -- logs '[retire] LEAK ref still held' if
         the bin survives our cleanup."""
    name = sub_bin.get_name() if sub_bin else "?"
    weak = weakref.ref(sub_bin)
    try:
        # (1) Disconnect closures FIRST -- this is what was missing.
        if pad_added_id:
            try:
                qtdemux_elem.disconnect(pad_added_id)
            except Exception as exc:
                print(f"[cutloop] retire {name} pad-added "
                      f"disconnect WARN: {exc}", file=sys.stderr)
        if eos_probe_id:
            try:
                concat_sink_pad.remove_probe(eos_probe_id)
            except Exception as exc:
                print(f"[cutloop] retire {name} probe remove "
                      f"WARN: {exc}", file=sys.stderr)
        # (3-5) Tear down the bin + release the concat pad.
        sub_bin.set_state(Gst.State.NULL)
        pipeline.remove(sub_bin)
        concat.release_request_pad(concat_sink_pad)
        live_subgraph_count[0] -= 1
        # Stamp the boundary timestamp so subsequent GAP-WARNs are
        # tagged BOUNDARY -- retire (NULL state-change) is also on
        # the hot path and can spike the single core.
        last_boundary_ns[0] = time.monotonic_ns()
        print(f"[cutloop] retire {name} "
              f"(live_bins={live_subgraph_count[0]})",
              file=sys.stderr)
    except Exception as exc:
        print(f"[cutloop] retire {name} WARN: {exc}",
              file=sys.stderr)
    # (7) Drop our local refs + schedule the leak check as a
    # SEPARATE idle so the idle source holding our refs has been
    # cleaned up before we test the weakref.
    del sub_bin, concat_sink_pad, qtdemux_elem

    def _check_leak():
        gc.collect()
        if weak() is not None:
            print(f"[cutloop] [retire] LEAK ref still held for "
                  f"{name}", file=sys.stderr)
        return False
    GLib.idle_add(_check_leak)
    return False  # one-shot


# Pre-queue INITIAL_QUEUE_DEPTH clips before starting playback.
for _ in range(INITIAL_QUEUE_DEPTH):
    add_next_clip()


# --- Instrument (minimal: frames/sec + gap-warn) -----------------------

counter = {"frames": 0, "last_ns": 0, "max_gap_ms": 0.0,
           "dec_frames": 0, "dec_last_ns": 0, "dec_max_gap_ms": 0.0,
           "boundary_warns_total": 0,
           "gap_warns_boundary": 0,
           "gap_warns_interior": 0}

# Per QA: tag each GAP-WARN with whether it falls within
# BOUNDARY_PROXIMITY_MS of the most recent add_next_clip or
# retire_subgraph call. If clustered at boundaries: sub-bin
# build/retire is spiking the single core (fix = move work off
# the hot path). If uniform/interior: kmssink/queue pacing on
# one core (fix = deeper jitter queue). The split-counter in
# the [fps] line tells us which.
BOUNDARY_PROXIMITY_MS = 200
last_boundary_ns = [0]  # monotonic ns of most recent add/retire

# Watchdog state: if kmssink.sink sees 0 frames for 2 consecutive
# seconds, concat queue may have run dry and the pipeline EOSed.
# Force a recovery (add fresh clip + re-PLAY). Per QA dispatch.
watchdog_state = {"zero_frame_seconds": 0}

try:
    log_file = open("/tmp/wipe_fps.log", "w", buffering=1)
    log_file.write(f"# cutloop run start pid={os.getpid()}\n")
    print("[cutloop] tee fps to /tmp/wipe_fps.log (truncated on open)",
          file=sys.stderr)
except OSError as _exc:
    log_file = None
    print(f"[cutloop] /tmp/wipe_fps.log unavailable ({_exc}); "
          "stderr only", file=sys.stderr)


def attach_kmssink_probe():
    sink_pad = sink.get_static_pad("sink")
    if sink_pad is None:
        die("kmssink has no sink pad")

    def _probe(_pad, _info):
        now = time.monotonic_ns()
        counter["frames"] += 1
        last = counter["last_ns"]
        if last:
            gap_ms = (now - last) / 1e6
            if gap_ms > counter["max_gap_ms"]:
                counter["max_gap_ms"] = gap_ms
            if gap_ms > GAP_WARN_MS:
                counter["boundary_warns_total"] += 1
                # Tag per QA: BOUNDARY if within
                # BOUNDARY_PROXIMITY_MS of the most recent
                # add_next_clip or retire_subgraph; else INTERIOR.
                since_boundary_ms = (
                    (now - last_boundary_ns[0]) / 1e6
                    if last_boundary_ns[0] else float("inf")
                )
                if since_boundary_ms < BOUNDARY_PROXIMITY_MS:
                    counter["gap_warns_boundary"] += 1
                    tag = (f"BOUNDARY (+{since_boundary_ms:.0f}ms "
                           "since add/retire)")
                else:
                    counter["gap_warns_interior"] += 1
                    tag = "INTERIOR (steady pacing)"
                print(f"[cutloop] GAP-WARN {gap_ms:.0f}ms "
                      f"(> {GAP_WARN_MS}ms threshold) {tag}",
                      file=sys.stderr)
        counter["last_ns"] = now
        return Gst.PadProbeReturn.OK

    sink_pad.add_probe(Gst.PadProbeType.BUFFER, _probe)


def attach_decoder_src_probe():
    """Per QA: count decoder output frames separately so we can
    localize a throttle to upstream (concat/decoder) vs downstream
    (kmssink). dec=N(gM) appears in the [fps] line beside the
    existing screen=frames=N(gM)."""
    dec_src = decoder.get_static_pad("src")
    if dec_src is None:
        die("v4l2h264dec has no src pad")

    def _probe(_pad, _info):
        now = time.monotonic_ns()
        counter["dec_frames"] += 1
        last = counter["dec_last_ns"]
        if last:
            gap_ms = (now - last) / 1e6
            if gap_ms > counter["dec_max_gap_ms"]:
                counter["dec_max_gap_ms"] = gap_ms
        counter["dec_last_ns"] = now
        return Gst.PadProbeReturn.OK

    dec_src.add_probe(Gst.PadProbeType.BUFFER, _probe)


_t_start = time.monotonic()


def fps_tick():
    uptime = int(time.monotonic() - _t_start)
    line = (f"[fps] t={uptime} "
            f"dec={counter['dec_frames']}"
            f"(g{int(counter['dec_max_gap_ms'])}) "
            f"screen={counter['frames']}"
            f"(g{int(counter['max_gap_ms'])}) "
            f"live_bins={live_subgraph_count[0]} "
            f"total_added={playlist_added_count[0]} "
            f"gap_warns_total={counter['boundary_warns_total']} "
            f"gap_boundary={counter['gap_warns_boundary']} "
            f"gap_interior={counter['gap_warns_interior']}")
    print(line, file=sys.stderr, flush=True)
    if log_file is not None:
        try:
            log_file.write(line + "\n")
        except OSError:
            pass
    # Watchdog per QA dispatch: 0 frames for 2 consecutive seconds
    # likely means concat ran out of pads + downstream EOS killed
    # the decoder. Force-recover by adding a fresh subgraph and
    # re-PLAYING. Check BEFORE the counter reset so we read the
    # just-completed second.
    if counter["frames"] == 0:
        watchdog_state["zero_frame_seconds"] += 1
        if watchdog_state["zero_frame_seconds"] >= 2:
            print("[cutloop] WATCHDOG: 0 frames for 2s -- "
                  "force-add fresh clip + re-PLAY",
                  file=sys.stderr)
            watchdog_state["zero_frame_seconds"] = 0
            GLib.idle_add(add_next_clip)
            GLib.idle_add(
                lambda: pipeline.set_state(Gst.State.PLAYING) or False
            )
    else:
        watchdog_state["zero_frame_seconds"] = 0
    counter["frames"] = 0
    counter["max_gap_ms"] = 0.0
    counter["dec_frames"] = 0
    counter["dec_max_gap_ms"] = 0.0
    return True


# --- Bus + signals -----------------------------------------------------


def on_bus(_bus, msg):
    if msg.type == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        src = msg.src.get_name() if msg.src else "?"
        print(f"[cutloop] ERROR {src}: {err.message}",
              file=sys.stderr)
        if dbg:
            print(f"[cutloop]  debug: {dbg}", file=sys.stderr)
        loop.quit()
    elif msg.type == Gst.MessageType.EOS:
        # Should never happen under steady-state: each source's EOS
        # triggers add_next_clip which keeps concat from emitting
        # downstream EOS. If we see it, the dynamic add lagged or
        # failed -- visible failure.
        print("[cutloop] pipeline EOS -- concat ran out of sources! "
              "(add_next_clip lagged or failed)", file=sys.stderr)
        loop.quit()


bus = pipeline.get_bus()
bus.add_signal_watch()
bus.connect("message", on_bus)


def shutdown(*_):
    GLib.idle_add(loop.quit)


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)


# --- Run ---------------------------------------------------------------

print("==RUN-START== cutloop", file=sys.stderr)
print(f"[cutloop] starting; {len(VIDEOS)} videos in playlist; "
      f"initial concat queue depth = {INITIAL_QUEUE_DEPTH}; "
      "ctrl-c to stop", file=sys.stderr)

if pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
    die("set_state PAUSED failed")
ret, cur, pending = pipeline.get_state(30 * Gst.SECOND)
if ret not in (Gst.StateChangeReturn.SUCCESS,
               Gst.StateChangeReturn.NO_PREROLL):
    die(f"preroll did not reach PAUSED: ret={ret.value_nick}")
print(f"[cutloop] preroll done; state={cur.value_nick}",
      file=sys.stderr)

if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
    die("set_state PLAYING failed")

attach_kmssink_probe()
attach_decoder_src_probe()
GLib.timeout_add(1000, fps_tick)

try:
    loop.run()
finally:
    print("[cutloop] shutdown -> NULL", file=sys.stderr)
    pipeline.set_state(Gst.State.NULL)
    if log_file is not None:
        try:
            log_file.close()
        except OSError:
            pass
