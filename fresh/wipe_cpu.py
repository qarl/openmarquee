#!/usr/bin/env python3
"""fresh/wipe_cpu.py -- A <-> B wipe via fresh-decoder-per-clip, NO seeks.

Step 2b pivot to the OLD-RENDERER proven pattern (2026-06-17).

WHY NOT SEEK-AND-RESERVOIR. The prior arc landed real-time looping
plus smooth mid-clip but hit two coupled problems at every loop
boundary: a ~1s screen stall (empty decode chain by the time the
idle re-seek ran, dominated by v4l2h264dec input -> first-output
refill latency on bcm2835) AND a frame discontinuity (the
non-flushing SEGMENT seek resets the producer PTS backward
4.75s/9.08s while the compositor running-time marches forward).
The "code" coder mined the old Rust renderer and found that the
proven gap-free technique used by hdmi.rs / playback.py is NOT a
buffer-depth trick (more buffering wedges the Zero 2 W CMA) -- it
is a fresh-decoder-per-clip architecture with NO seeks.

THE PROVEN PATTERN (this file).

  1. NO SEEKS. Each clip play = a FRESH decode bin (filesrc ->
     qtdemux -> h264parse -> v4l2h264dec -> videorate -> caps ->
     queue -> intervideosink channel=ch?), played ONCE to EOS, then
     torn down. The "loop" = a SEQUENCE of fresh bins on the same
     channel. Each bin has monotonic PTS from 0 -> no discontinuity
     by construction.

  2. PRELOAD ~1s ahead. When the currently-playing bin crosses the
     preload threshold (PTS >= dur - WIPE_S - PRELOAD_LEAD), spawn
     the NEXT clip's fresh bin on the OTHER channel and set it to
     PAUSED. Preroll decodes its first frames offscreen so it is
     WARM by the time the cross-fade starts.

  3. CROSS-FADE. When the currently-playing bin crosses the wipe
     threshold (PTS >= dur - WIPE_S), set the preloaded bin to
     PLAYING and start the wipe animation. Both bins are live + the
     compositor reads from both for WIPE_S seconds.

  4. TEAR DOWN on EOS. The outgoing bin hits EOS at roughly the end
     of the wipe; its pipeline is set to NULL and unreferenced. The
     channel is now free for the next cycle on the same channel.

  5. GAP-KILLER (safety net). If on_wipe_threshold fires and no
     preloaded next bin is ready (cold), spawn an emergency bin
     synchronously and start the wipe -- prefer a brief stall over a
     missed transition. Logged loudly.

  6. TIGHT BUFFERS. 4-buffer queue between videorate and
     intervideosink. NO large reservoir. CMA stays modest (~30MB per
     active bin at 720p NV12); two active bins during the wipe
     overlap = ~60MB peak.

COMPOSITOR PIPELINE (pipe_c -- always running, never re-built):

  intervideosrc channel=chA timeout=200ms do-timestamp=TRUE
    ! videorate skip-to-first=true ! framerate=30/1
    ! queue (leaky downstream) ! comp.sink_0
  intervideosrc channel=chB ... ! comp.sink_1
  compositor name=comp background=black
    ! width=1280,height=720,framerate=30/1
    ! videoconvert ! NV12@1280x720
    ! kmssink name=sink sync=true   (HW-scales 1280x720 -> 1360x768
                                     via the vc4 DRM plane)

  do-timestamp=TRUE on intervideosrc is the DISCONTINUITY FIX. Each
  fresh ClipBin restarts PTS at 0, while the compositor running-
  time has marched forward across previous clips. Without re-stamp,
  kmssink sync=true would treat the new clip's first second as
  "late" and drop frames -- the visible discontinuity. With
  do-timestamp=true, intervideosrc re-stamps each pulled buffer
  with the CURRENT compositor running-time, so the consumer sees a
  monotonic timeline regardless of how many bins have come and gone
  upstream.

DECODE BIN PER CLIP (spawned fresh, torn down at EOS):

  filesrc -> qtdemux -> h264parse -> v4l2h264dec
    -> videorate ! framerate=30/1
    -> queue max-size-buffers=4 (non-leaky)
    -> intervideosink channel=ch? sync=true async=false

  sync=true paces decode to real-time via the SHARED clock the
  ClipBin sets on the pipeline at build time (use_clock +
  set_base_time matching pipe_c). async=false avoids the async
  preroll wait on this non-display sink.

RISK + WATCH. v4l2h264dec bin teardown must cleanly release the
bcm2835 CAPTURE buffers each cycle. If gstreamer leaks V4L2 slots
on .set_state(NULL) + unref, continuous fresh-bin cycling will
eventually wedge the codec (REQBUFS EINVAL). The old Rust renderer
needed explicit eviction fixes (r102.x) to make per-clip teardown
safe. QA watches CmaFree + dmesg over many cycles; if leaks appear,
fallback is segment-seek + reservoir (which keeps the
discontinuity, so this is the preferred path).

INSTRUMENT. Per-second [fps] line continues to print to stderr + a
/tmp/wipe_fps.log tee. Probes attach to the COMPOSITOR pipeline's
intervideosrc src pads (counting frames reaching the compositor)
and kmssink sink pad (counting frames painted). Per-bin probes are
not used for fps counting because bins come and go; the compositor-
side probes are stable. Output:

  [fps] t=12 inA=30 inB=29 screen=30 \
        main=A#3 next=- pts=2.43 state=HOLD

  inA/inB  = frames reaching compositor on chA/chB this second
  screen   = frames into kmssink.sink this second
  main     = label of the currently-playing main bin (slot#serial)
  next     = label of the preloaded next bin, or "-"
  pts      = main bin position in its clip (seconds)
  state    = HOLD or WIPE
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
WIPE_S = 1.0
TICK_MS = 33
PREROLL_BUDGET_S = 30

# Hardcoded ns constants (Gst.SECOND at module-import time is
# pre-Gst.init, may raise on some PyGObject versions).
PRELOAD_LEAD_NS = 1_000_000_000  # 1.0s -- per old renderer
                                 # OPENMARQUEE_PRELOAD_LEAD_MS=1000
WIPE_S_NS = int(WIPE_S * 1_000_000_000)

# Fallback durations if query_duration on the demuxer fails.
DUR_FALLBACK_NS = {
    "A": 4_750_000_000,  # 4.75s
    "B": 9_080_000_000,  # 9.08s
}

# Leak-prevention fallback toggle (per QA spec). OFF by default --
# the primary defense is the 250ms teardown grace + bus.remove_signal
# _watch in ClipBin.teardown + event-gated teardown (only after the
# wipe completes) + T5 all-three queue caps EVERYWHERE.
#
# If a long QA leak-soak ever shows CMA drift over many cycles, set
# this env var to "1" to insert a videoconvert before each decode
# bin's intervideosink. Intent: copy NV12 into a NEW non-V4L2 buffer
# pool, severing the back-ref chain from intervideo/compositor/
# kmssink to the dying decoder's CAPTURE pool.
#
# CAVEAT (QA guardrail #3): a NV12 -> NV12 videoconvert can detect
# matching caps and PASSTHROUGH (zero-copy), which preserves the
# back-ref and defeats the fallback. If this toggle is ever flipped
# on and CMA still drifts, we will need to either (a) force a caps
# transition that requires an actual copy (e.g. NV12 -> I420 ->
# NV12), or (b) use a copying element such as gdpdepay or appsink+
# appsrc to guarantee buffer-pool detachment. Not implemented now.
#
# Cost per frame at 720p: ~42MB/s per bin, doubled during the
# crossfade overlap -- exactly the per-frame pixel work that the
# videoscale removal lifted. Do NOT flip on without leak evidence.
INSERT_TEARDOWN_VIDEOCONVERT = (
    os.environ.get("OPENMARQUEE_VIDEOCONVERT_BEFORE_INTERVIDEOSINK",
                   "0").lower() in ("1", "true", "yes")
)

# QA guardrail #4 (note-only, not enabled): v4l2h264dec capture-io-
# mode=dmabuf could decouple the MMAL slot lifecycle from buffer
# refs entirely. Two reasons not to enable now:
#   - Our CPU compositor must read pixels, so it would map the
#     dmabuf back to system memory anyway -- no zero-copy win on
#     this path.
#   - bcm2835 slot-free-with-fds-outstanding behavior is
#     unverified on this build; could change the leak surface in
#     surprising ways.
# Note for the QA-soak follow-up if B leaks despite the
# guardrails: evaluate v4l2h264dec capture-io-mode=dmabuf as the
# next lever.


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
    ["pgrep", "-af",
     "v4l2h264dec|openmarquee-render|mini-play|wipe.py|wipe_cpu"],
    capture_output=True, text=True,
)
peers = [
    line for line in ps.stdout.splitlines()
    if line.split() and line.split()[0] not in own_pids
]
if peers:
    die("another HW-decode process is already running:\n  "
        + "\n  ".join(peers))

# --- Compositor pipeline (always running) ------------------------------

NV12_DISP = f"video/x-raw,format=NV12,width={DW},height={DH}"
RATE_30 = f"video/x-raw,framerate={FPS}/1"


def build_compositor_pipeline():
    desc = (
        f"compositor name=comp background=black "
        f"  sink_0::xpos=0 sink_0::ypos=0 "
        f"  sink_0::width={DW} sink_0::height={DH} sink_0::zorder=0 "
        f"  sink_1::xpos=0 sink_1::ypos=0 "
        f"  sink_1::width=0 sink_1::height={DH} sink_1::zorder=1 "
        f"! video/x-raw,width={DW},height={DH},framerate={FPS}/1 "
        # T5 all-three caps: bound the buffer count AND bytes AND
        # time. Default max-size-bytes=10MB ~= 7 NV12 720p frames;
        # default max-size-time=1s ~= 30 frames. Either alone
        # silently keeps ~30 v4l2-pool-backed buffers in flight ->
        # MMAL slot leak on bin teardown. Cap all three to the
        # smallest of (4 buffers, 0 bytes-unlimited, 0 time-
        # unlimited), so only the 4-buffer cap actually gates.
        f"! queue max-size-buffers=4 max-size-bytes=0 "
        f"  max-size-time=0 leaky=downstream "
        f"! videoconvert "
        f"! {NV12_DISP} "
        f"! kmssink name=sink sync=true "
        # DO NOT RAISE this timeout near/above the cross-fade
        # duration. intervideosrc holds the last upstream
        # (v4l2-pool) buffer for timeout x fps frames; at 200ms =
        # 6 frames it releases ~970ms before teardown (safe).
        # >~1300ms would pin the decoder pool across a teardown ->
        # MMAL slot leak. (code source-verified in
        # gst-plugins-bad.)
        f"intervideosrc name=srcA channel=chA "
        f"  timeout=200000000 do-timestamp=true "
        f"  ! videorate skip-to-first=true ! {RATE_30} "
        f"  ! queue max-size-buffers=4 max-size-bytes=0 "
        f"    max-size-time=0 leaky=downstream "
        f"  ! comp.sink_0 "
        f"intervideosrc name=srcB channel=chB "
        f"  timeout=200000000 do-timestamp=true "
        f"  ! videorate skip-to-first=true ! {RATE_30} "
        f"  ! queue max-size-buffers=4 max-size-bytes=0 "
        f"    max-size-time=0 leaky=downstream "
        f"  ! comp.sink_1"
    )
    try:
        return Gst.parse_launch(desc)
    except GLib.Error as exc:
        die(f"compositor pipeline parse failed: {exc.message}")


pipe_c = build_compositor_pipeline()

comp = pipe_c.get_by_name("comp")
sink = pipe_c.get_by_name("sink")
srcA = pipe_c.get_by_name("srcA")
srcB = pipe_c.get_by_name("srcB")
for label, el in [("comp", comp), ("sink", sink),
                  ("srcA", srcA), ("srcB", srcB)]:
    if el is None:
        die(f"required element {label} not found in compositor pipeline")


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


# pads[0] = comp.sink_0 (slot A), pads[1] = comp.sink_1 (slot B).
# Static for the script lifetime; do not regenerate per bin.
pads = [find_sink_pad(comp, "sink_0"), find_sink_pad(comp, "sink_1")]
if pads[0] is None or pads[1] is None:
    die(f"could not locate compositor pads sink_0/sink_1: {pads}")


# --- Shared clock + base time (set once, applied to every bin) ---------

shared_clock = Gst.SystemClock.obtain()
shared_base_time = None  # set after compositor PAUSED


# --- ClipBin: a short-lived decode pipeline ----------------------------

# Slot mapping: A=chA=sink_0, B=chB=sink_1. The wipe direction
# alternates which slot is "outgoing" vs "incoming".
SLOT_INFO = {
    "A": {"asset": VIDEOS[0], "channel": "chA", "pad_idx": 0},
    "B": {"asset": VIDEOS[1], "channel": "chB", "pad_idx": 1},
}

_serial_counter = [0]


def _next_serial():
    _serial_counter[0] += 1
    return _serial_counter[0]


def opposite_slot(slot_name):
    return "B" if slot_name == "A" else "A"


class ClipBin:
    """One decode pipeline for one clip play. Build -> preroll
    (PAUSED) -> play (PLAYING) -> EOS -> teardown (NULL + unref).
    Triggers (preload, wipe-start) fire from a BUFFER probe on the
    demuxer src pad."""

    def __init__(self, slot_name):
        s = SLOT_INFO[slot_name]
        self.slot = slot_name
        self.asset = s["asset"]
        self.channel = s["channel"]
        self.serial = _next_serial()
        self.label = f"{slot_name}#{self.serial}"
        self.pipeline = None
        self.demuxer = None
        self.intersink = None
        self.dur_ns = DUR_FALLBACK_NS[slot_name]
        self.preload_fired = False
        self.wipe_fired = False
        self.eos_dispatched = False
        # S1: teardown is gated on three independent events; this
        # flag dedupes the schedule call across them.
        self.teardown_scheduled = False
        # Per qtdemux dynamic-pad race fix: pad-added on the demuxer
        # fires once when qtdemux exposes its video pad. We install
        # the trigger BUFFER probe + query_duration FROM that
        # callback (not synchronously after preroll, which races the
        # dynamic pad because intervideosink async=false returns
        # SUCCESS before qtdemux has parsed moov).
        self.trigger_probe_installed = False
        # Cross-fade readiness gates (asymmetric -- only checked on
        # the INCOMING bin before starting the wipe).
        self.async_done = False    # G1: ASYNC_DONE on bus
        self.first_buffer = False  # G2: at least one buffer past
                                   # the intervideosink sink pad

    def build(self):
        # Optional videoconvert before intervideosink (leak fallback).
        leak_convert = ("! videoconvert "
                        if INSERT_TEARDOWN_VIDEOCONVERT else "")
        desc = (
            f'filesrc location="{self.asset}" name=src '
            f"! qtdemux name=demux ! h264parse "
            f"! v4l2h264dec name=dec "
            f"! videorate ! {RATE_30} "
            f"! queue max-size-buffers=4 max-size-bytes=0 "
            f"  max-size-time=0 "
            f"{leak_convert}"
            f"! intervideosink channel={self.channel} "
            f"  sync=true async=false"
        )
        try:
            self.pipeline = Gst.parse_launch(desc)
        except GLib.Error as exc:
            die(f"[{self.label}] parse failed: {exc.message}")
        self.demuxer = self.pipeline.get_by_name("demux")
        if self.demuxer is None:
            die(f"[{self.label}] demux not found post-parse")
        # Find intervideosink via element iteration (no explicit
        # name= in the desc).
        it = self.pipeline.iterate_elements()
        while True:
            res, el = it.next()
            if res != Gst.IteratorResult.OK:
                break
            fac = el.get_factory()
            if fac and fac.get_name() == "intervideosink":
                self.intersink = el
                break
        if self.intersink is None:
            die(f"[{self.label}] intervideosink not found post-parse")
        # Pin clock + base-time BEFORE state change so PLAYING does
        # not auto-select something else.
        self.pipeline.use_clock(shared_clock)
        if shared_base_time is not None:
            self.pipeline.set_base_time(shared_base_time)
        # Bus handler for ASYNC_DONE + EOS + ERROR.
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)
        # Gate G2: first-buffer probe on intervideosink sink pad.
        sink_pad = self.intersink.get_static_pad("sink")
        if sink_pad is None:
            die(f"[{self.label}] intervideosink has no sink pad")

        def _on_first_buffer(_pad, _info):
            if not self.first_buffer:
                self.first_buffer = True
                print(f"[{self.label}] first buffer past "
                      "intervideosink sink (G2 ready)")
            return Gst.PadProbeReturn.OK

        sink_pad.add_probe(Gst.PadProbeType.BUFFER, _on_first_buffer)
        # Decoded-frame probe (per QA diagnostic): BUFFER probe on
        # v4l2h264dec src pad increments the per-SLOT decoded counter.
        # Surfaced as decA / decB in the [fps] line. Distinguishes
        # "decoder never driven" (dec=0, only intervideosrc fallback
        # to compositor) from "frames flow + bug elsewhere" (dec~30).
        decoder = self.pipeline.get_by_name("dec")
        if decoder is None:
            die(f"[{self.label}] v4l2h264dec named dec not found")
        dec_src = decoder.get_static_pad("src")
        if dec_src is None:
            die(f"[{self.label}] v4l2h264dec has no src pad")
        slot_key = self.slot
        gs = dec_gap_state[slot_key]

        def _on_dec_buffer(_pad, _info):
            dec_counters[slot_key] += 1
            now_ns_local = time.monotonic_ns()
            last = gs["last_ns"]
            if last:
                gap_ms = (now_ns_local - last) / 1e6
                if gap_ms > gs["max_gap_ms"]:
                    gs["max_gap_ms"] = gap_ms
            gs["last_ns"] = now_ns_local
            return Gst.PadProbeReturn.OK

        dec_src.add_probe(Gst.PadProbeType.BUFFER, _on_dec_buffer)
        # qtdemux dynamic-pad: connect pad-added BEFORE state change
        # so we do not miss the signal regardless of preroll timing.
        self.demuxer.connect("pad-added", self._on_demux_pad_added)
        return self

    def preroll(self):
        """Set PAUSED + block-poll for state. File-backed source
        prerolls a frame -> SUCCESS via ASYNC. Returns True if
        reached PAUSED.

        On SUCCESS we set async_done=True directly: the ASYNC_DONE
        bus message can race the wipe deadline-poll because spawn/
        preroll runs inside a GLib.idle_add callback that blocks the
        main loop's message dispatch. preroll SUCCESS already proves
        async-state-change completion, so treat it as G1."""
        ret = self.pipeline.set_state(Gst.State.PAUSED)
        if ret == Gst.StateChangeReturn.FAILURE:
            die(f"[{self.label}] failed to enter PAUSED")
        for elapsed in range(1, PREROLL_BUDGET_S + 1):
            if shutdown_requested:
                return False
            r, cur, pending = self.pipeline.get_state(1 * Gst.SECOND)
            if r == Gst.StateChangeReturn.SUCCESS:
                print(f"[{self.label}] preroll done after ~{elapsed}s "
                      f"state={cur.value_nick}")
                self.async_done = True  # G1 satisfied; do not wait on
                                        # bus dispatch.
                return True
            if r == Gst.StateChangeReturn.NO_PREROLL:
                print(f"[{self.label}] preroll NO_PREROLL "
                      "(unexpected for file source)")
                self.async_done = True
                return True
            if r == Gst.StateChangeReturn.FAILURE:
                die(f"[{self.label}] preroll FAILURE")
        die(f"[{self.label}] preroll exceeded {PREROLL_BUDGET_S}s budget")

    def query_duration(self):
        """DEPRECATED no-op kept for compat. Duration is now queried
        inside _on_demux_pad_added, which avoids the qtdemux dynamic-
        pad race with intervideosink async=false. Calling this
        synchronously after preroll RAN BEFORE qtdemux had parsed
        moov -> always failed -> fell back to DUR_FALLBACK_NS even
        though the real value would have been available a few ms
        later via pad-added."""
        pass

    def _on_demux_pad_added(self, _demux, pad):
        """qtdemux dynamic-pad callback. Fires on a streaming thread
        when qtdemux finishes parsing moov and exposes a src pad
        (once per stream -- video + maybe audio). Filter by caps
        structure name OR pad name so audio/subtitle pads are
        ignored and a NULL-caps pad on rare qtdemux paths is still
        accepted as the video pad.

        Lightweight ops only per QA note (add_probe, query_duration,
        compute thresholds, set flags). No spawns / set_state here
        -- those would deadlock on the streaming thread; the
        scheduler defers all heavy work via GLib.idle_add already."""
        # Early-entry log (G7 diagnostic ask): proves _on_demux_pad_
        # added fired for THIS bin regardless of whether the filter
        # downstream accepts it. If the journal shows
        # "[A#1] pad-added pad=..." then qtdemux is exposing pads
        # correctly; if it's missing, pad-added itself is not
        # firing (qtdemux problem upstream).
        pad_name = pad.get_name()
        caps = pad.get_current_caps() or pad.query_caps(None)
        # REAL-ROOT FIX (per QA dispatch with run-scoped log proof):
        # caps.get_structure(0).get_name() raises
        #   AttributeError: 'StructureWrapper' object has no
        #   attribute 'get_name'
        # in this PyGObject/gst-python build. The exception silently
        # aborted the handler -> trigger_probe_installed never set
        # -> probe never attached -> preload/wipe never fired ->
        # gap-killer hard-cut every clip. Binding-portable fix:
        # use caps.to_string() (returns e.g.
        # "video/x-h264, parsed=(boolean)true, ..."), then take the
        # first media-type token before any comma/semicolon. Works
        # on every PyGObject version because to_string returns a
        # plain str.
        caps_str = caps.to_string() if caps else ""
        media_type = (
            caps_str.split(",", 1)[0].split(";", 1)[0].strip()
        )
        print(f"[{self.label}] pad-added pad={pad_name} "
              f"caps={media_type or 'NONE'}", file=sys.stderr)
        if self.trigger_probe_installed:
            return
        # G4 (per QA dispatch): robust caps filter. caps may be NULL
        # at pad-added on some qtdemux paths; pad name (qtdemux
        # convention "video_0") is the binding-independent backup.
        # Either signal is sufficient to identify the video pad.
        is_video = (
            media_type.startswith("video/")
            or pad_name.startswith("video")
        )
        if not is_video:
            print(f"[{self.label}] pad-added skip non-video "
                  f"(pad={pad_name} caps={media_type or 'NONE'})",
                  file=sys.stderr)
            return
        self.trigger_probe_installed = True
        # R3: query duration on the DEMUXER, not the pipeline.
        # qtdemux is the canonical duration source for an mp4 file;
        # asking the pipeline routes the query the same place
        # eventually but goes through more elements.
        ok, dur = self.demuxer.query_duration(Gst.Format.TIME)
        if ok and dur > 0:
            self.dur_ns = dur
            print(f"[{self.label}] duration = {dur} ns "
                  f"({dur / 1e9:.3f}s) [pad-added, caps="
                  f"{media_type}]", file=sys.stderr)
        else:
            print(f"[{self.label}] duration query on demuxer failed "
                  f"in pad-added; keeping fallback {self.dur_ns} ns "
                  f"({self.dur_ns / 1e9:.3f}s)", file=sys.stderr)
        # Cosmetic note (per QA secondary): the trigger probe sits
        # on the qtdemux src (pre-decode); G2 (first_buffer on the
        # intervideosink sink) is post-decode. They lag by the
        # ~70-270ms decode prime, so the "preload trigger" log may
        # appear BEFORE G2 is ready. Not a bug -- the gates have
        # plenty of margin (1s preload lead + 1s wipe).
        self._attach_trigger_probe(pad)

    def _attach_trigger_probe(self, pad):
        """BUFFER probe on demuxer src pad: fires preload threshold +
        wipe-start threshold each ONCE. Pad comes from pad-added."""
        # G5 (per QA dispatch): clamp / short-dur guard. If dur is
        # bogus or shorter than (preload_lead + wipe), the computed
        # thresholds go <= 0 and the first buffer fires both -- a
        # permanent dual-decode + immediate hard cut. Our actual
        # clips (A=4.75s, B=9.08s) are fine, but DUR_FALLBACK_NS
        # could be wrong and a future asset might be short. Skip
        # arming in that case; the gap-killer in on_bin_eos will
        # cleanly hard-cut at natural EOS.
        min_arm_dur = WIPE_S_NS + PRELOAD_LEAD_NS
        if self.dur_ns <= min_arm_dur:
            print(f"[{self.label}] dur={self.dur_ns / 1e9:.2f}s "
                  f"<= preload+wipe={min_arm_dur / 1e9:.2f}s -- "
                  "skipping trigger arm (EOS gap-killer will handle)",
                  file=sys.stderr)
            return
        preload_at = self.dur_ns - WIPE_S_NS - PRELOAD_LEAD_NS
        wipe_at = self.dur_ns - WIPE_S_NS

        def _on_buffer(_pad, info):
            buf = info.get_buffer()
            if buf is None or buf.pts == Gst.CLOCK_TIME_NONE:
                return Gst.PadProbeReturn.OK
            pts = buf.pts
            if not self.preload_fired and pts >= preload_at:
                self.preload_fired = True
                print(f"[{self.label}] preload trigger at "
                      f"pts={pts / 1e9:.2f}s "
                      f"(>={preload_at / 1e9:.2f}s)",
                      file=sys.stderr)
                GLib.idle_add(on_preload_trigger, self)
            if not self.wipe_fired and pts >= wipe_at:
                self.wipe_fired = True
                print(f"[{self.label}] wipe trigger at "
                      f"pts={pts / 1e9:.2f}s "
                      f"(>={wipe_at / 1e9:.2f}s)",
                      file=sys.stderr)
                GLib.idle_add(on_wipe_trigger, self)
            return Gst.PadProbeReturn.OK

        pad.add_probe(Gst.PadProbeType.BUFFER, _on_buffer)
        print(f"[{self.label}] trigger probe attached on "
              f"{pad.get_name()} (preload>={preload_at / 1e9:.2f}s, "
              f"wipe>={wipe_at / 1e9:.2f}s)",
              file=sys.stderr)

    def install_trigger_probe(self):
        """DEPRECATED no-op kept for compat. Probe installation
        happens inside _on_demux_pad_added (via _attach_trigger_probe)
        so it cannot race the qtdemux dynamic pad. The old version
        of this method called find_video_src_pad synchronously after
        preroll, which returned None because qtdemux hadn't yet
        parsed moov (intervideosink async=false makes set_state
        return SUCCESS without waiting for the first buffer)."""
        pass

    def play(self):
        if (self.pipeline.set_state(Gst.State.PLAYING)
                == Gst.StateChangeReturn.FAILURE):
            die(f"[{self.label}] failed to enter PLAYING")
        print(f"[{self.label}] PLAYING")

    def teardown(self):
        """Teardown sequence per QA leak-prevention spec:
          T1: caller must be on main GLib thread (NOT a streaming
              thread / pad probe -- streaming-thread NULL deadlocks).
              Enforced upstream: _deferred_teardown runs via
              GLib.timeout_add, on_bin_eos is itself dispatched via
              GLib.idle_add from the bus handler.
          T3: explicitly detach the bus signal watch + null every
              `self.*` element reference here. The bus closure
              captures `self` (via _on_bus method), which keeps
              self.intersink alive even after pipeline=None, which
              keeps the downstream peer pinned -- exactly the leak
              vector code flagged.
          T5: the decode-bin queue is capped max-size-buffers=4
              (build_decode_pipeline) so only ~4 back-refs need
              to drain.
        """
        if self.pipeline is None:
            return
        print(f"[{self.label}] teardown -> NULL")
        # Detach bus signal first so _on_bus does not fire on
        # state-change messages during NULL transition.
        bus = self.pipeline.get_bus()
        if bus is not None:
            try:
                bus.remove_signal_watch()
            except Exception as exc:
                print(f"[{self.label}] WARN bus.remove_signal_watch "
                      f"failed: {exc}", file=sys.stderr)
        self.pipeline.set_state(Gst.State.NULL)
        # Wait for NULL so V4L2 CAPTURE slots are actually released
        # before another bin tries to take the channel. Per spec
        # cap=1s; log + force-continue if exceeded.
        r, _, _ = self.pipeline.get_state(1 * Gst.SECOND)
        if r != Gst.StateChangeReturn.SUCCESS:
            print(f"[{self.label}] WARN teardown to NULL not "
                  f"confirmed within 1s: ret={r.value_nick} "
                  "-- proceeding with unref anyway",
                  file=sys.stderr)
        # T3: drop every element ref so the closure-holds chain
        # cannot pin a downstream peer.
        self.pipeline = None
        self.demuxer = None
        self.intersink = None

    def query_position_ns(self):
        if self.pipeline is None:
            return -1
        ok, pos = self.pipeline.query_position(Gst.Format.TIME)
        return pos if (ok and pos >= 0) else -1

    def _on_bus(self, _bus, msg):
        if msg.type == Gst.MessageType.ASYNC_DONE:
            if not self.async_done:
                self.async_done = True
                print(f"[{self.label}] ASYNC_DONE (G1 ready)")
            return
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            src = msg.src.get_name() if msg.src else "?"
            print(f"[{self.label}] ERROR {src}: {err.message}",
                  file=sys.stderr)
            if dbg:
                print(f"[{self.label}]  debug: {dbg}",
                      file=sys.stderr)
            loop.quit()
            return
        if msg.type == Gst.MessageType.EOS:
            if not self.eos_dispatched:
                self.eos_dispatched = True
                print(f"[{self.label}] EOS")
                GLib.idle_add(on_bin_eos, self)


# --- Slot state machine ------------------------------------------------

# Slot state. Always has at most one bin in each slot.
slot_state = {
    "main": None,        # ClipBin currently being shown full-screen
    "next": None,        # ClipBin preloaded for next transition
    "outgoing": None,    # ClipBin being torn down (post-wipe, pre-EOS)
}

# Wipe state. Mirrors the prior code's HOLD/WIPE animation timer.
#
# post_wipe_frame_emitted is the S1 gate. Lifecycle per cycle:
#   start of cycle (no wipe pending): True (default below).
#   _start_wipe_animation: flip to False at wipe begin.
#   screen counter probe (kmssink.sink): flip back to True on the
#     first buffer that paints AFTER phase has returned to HOLD --
#     proves the FINAL composited frame actually emitted past the
#     sink, not just that the alpha-animation timer expired.
#
# CLARIFICATION on the initial value True: it does NOT short-
# circuit the teardown gate. try_schedule_teardown_for_outgoing
# first checks `out = slot_state.get("outgoing")`; at startup
# there is no outgoing yet, so the gate returns False there
# regardless of this flag's value. The True default only matters
# for the very first cycle where the flag is read before any wipe
# has occurred -- consistent with "no wipe-in-flight, no missed
# post-wipe frame to wait for".
wipe_state = {
    "phase": "HOLD",                    # "HOLD" or "WIPE"
    "start_ns": 0,                      # monotonic_ns when WIPE began
    "incoming_pad_idx": -1,             # 0 or 1
    "outgoing_pad_idx": -1,
    "post_wipe_frame_emitted": True,    # S1 gate -- see above
}


def now_ns():
    return GLib.get_monotonic_time() * 1000


def set_geom(pad, x, w):
    pad.set_property("xpos", int(x))
    pad.set_property("width", int(w))


def on_preload_trigger(bin_):
    """Main bin crossed preload threshold. Spawn the OTHER slot
    fresh + PAUSED. Skip if already preloaded (e.g. trigger fired
    twice across a stale probe)."""
    if bin_ is not slot_state["main"]:
        print(f"[sched] preload from {bin_.label} but main is "
              f"{slot_state['main'].label if slot_state['main'] else None}"
              " -- stale, ignoring")
        return False
    if slot_state["next"] is not None:
        print(f"[sched] preload from {bin_.label} but next already "
              f"= {slot_state['next'].label} -- ignoring")
        return False
    other = opposite_slot(bin_.slot)
    print(f"[sched] preload {other} (lead by {bin_.label})")
    nxt = ClipBin(other).build()
    nxt.preroll()
    # Trigger probe + duration wired via pad-added on the demuxer
    # (fires within ms of PAUSED, BEFORE we ever need either value).
    slot_state["next"] = nxt
    return False  # one-shot idle_add


CROSSFADE_POLL_MS = 2     # sleep between gate-checks
CROSSFADE_DEADLINE_MS = 100  # cap; start wipe anyway after this


def on_wipe_trigger(bin_):
    """Main bin crossed wipe threshold. Set the preloaded next bin
    PLAYING, then deadline-poll the two readiness gates (G1
    ASYNC_DONE, G2 first buffer past intervideosink) before starting
    the wipe animation. Gap-killer: if no preloaded next is ready,
    emergency-spawn (logged loudly). Asymmetric: never gate the
    outgoing bin -- it is warm by definition."""
    if bin_ is not slot_state["main"]:
        print(f"[sched] wipe from {bin_.label} but main is "
              f"{slot_state['main'].label if slot_state['main'] else None}"
              " -- stale, ignoring")
        return False
    nxt = slot_state["next"]
    if nxt is None:
        other = opposite_slot(bin_.slot)
        print(f"[sched] GAP-KILLER wipe trigger with no preloaded "
              f"next -- emergency spawn {other}", file=sys.stderr)
        nxt = ClipBin(other).build()
        nxt.preroll()
        # Trigger probe + duration are wired via pad-added (fires
        # within ms of PAUSED). No synchronous post-preroll work.
        slot_state["next"] = nxt
    nxt.play()
    # Deadline-poll the gates on the next bin; never on the
    # outgoing. The poll callback runs on the main GLib thread
    # (GLib.timeout_add), so it does not block the streaming
    # threads that drive the gates.
    deadline_ns = now_ns() + CROSSFADE_DEADLINE_MS * 1_000_000

    def _check_gates():
        ready = nxt.async_done and nxt.first_buffer
        if ready:
            _start_wipe_animation(bin_, nxt)
            return False  # stop polling
        if now_ns() >= deadline_ns:
            print(f"[sched] {nxt.label} wipe DEADLINE "
                  f"exhausted ({CROSSFADE_DEADLINE_MS}ms) -- "
                  f"async_done={nxt.async_done} "
                  f"first_buffer={nxt.first_buffer}; "
                  "starting wipe anyway",
                  file=sys.stderr)
            _start_wipe_animation(bin_, nxt)
            return False
        return True  # keep polling

    GLib.timeout_add(CROSSFADE_POLL_MS, _check_gates)
    return False


def _start_wipe_animation(outgoing_bin, incoming_bin):
    in_idx = SLOT_INFO[incoming_bin.slot]["pad_idx"]
    out_idx = SLOT_INFO[outgoing_bin.slot]["pad_idx"]
    pads[in_idx].set_property("zorder", 1)
    pads[out_idx].set_property("zorder", 0)
    set_geom(pads[in_idx], 0, 0)
    wipe_state["phase"] = "WIPE"
    wipe_state["start_ns"] = now_ns()
    wipe_state["incoming_pad_idx"] = in_idx
    wipe_state["outgoing_pad_idx"] = out_idx
    # S1: a new wipe begins, so the "final composited frame past
    # kmssink" event hasn't happened yet for this cross-fade.
    wipe_state["post_wipe_frame_emitted"] = False
    print(f"[sched] WIPE {outgoing_bin.label} -> {incoming_bin.label}")


# Grace before teardown: let in-flight buffer refs flush through the
# intervideo bridge so the dying decoder's last dmabuf does not get
# wrapped by a still-queued compositor buffer (which would keep the
# v4l2h264dec fd open -> bcm2835 MMAL slot leak). Per QA leak-spec:
# 250ms is the baseline grace that empirically covers the bridge +
# compositor + kmssink in-flight ref-drain on Pi Zero 2 W at 30fps.
TEARDOWN_GRACE_MS = 250


def on_bin_eos(bin_):
    """S3: bus EOS only updates bookkeeping + flags. The actual
    teardown schedule is gated by try_schedule_teardown_for_outgoing
    which checks ALL THREE conditions (eos, post-wipe-frame, phase).
    Calling _schedule_teardown directly from here would defeat S1
    (the post-wipe-frame condition) when EOS lands post-wipe-timer
    but before the next composited frame is actually painted.

    GAP-KILLER at the EOS boundary: if no `next` was preloaded (the
    preload_trigger never fired for this main, OR fired too late),
    spawn the opposite-slot bin SYNCHRONOUSLY here so the scheduler
    never lands in slot_state["main"] = None / next = None / dead.
    Per QA soak diagnosis (ace7b20): without this, a single missed
    preload kills the entire system forever. With it, the first
    cycle may have a visible cut (no wipe, just a hard switch via
    the synchronous spawn), but the scheduler cannot wedge -- on
    the next clip the normal preload_trigger has another chance to
    fire and restore smooth crossfades."""
    print(f"[sched] EOS handler: {bin_.label}")
    if bin_ is slot_state.get("main"):
        slot_state["outgoing"] = bin_
        new_main = slot_state["next"]
        if new_main is None:
            other = opposite_slot(bin_.slot)
            print(f"[sched] EOS-GAP-KILLER no preloaded next -- "
                  f"spawn {other} synchronously to keep slot alive",
                  file=sys.stderr)
            new_main = ClipBin(other).build()
            new_main.preroll()
            new_main.play()
        slot_state["main"] = new_main
        slot_state["next"] = None
    elif bin_ is slot_state.get("outgoing"):
        pass  # already moved aside via prior path
    else:
        print(f"[sched] EOS from unknown role {bin_.label}",
              file=sys.stderr)
    try_schedule_teardown_for_outgoing()
    return False


def try_schedule_teardown_for_outgoing():
    """S1+S3 triple gate. Schedules teardown of the outgoing bin
    only when ALL of:
      - outgoing exists (slot_state has been promoted)
      - outgoing.eos_dispatched (bus EOS has fired)
      - wipe_state.phase == HOLD (animation timer hit final)
      - wipe_state.post_wipe_frame_emitted (the FIRST kmssink-sink
        buffer AFTER phase returned to HOLD has been observed --
        proves the final composited frame actually painted, not
        just that the alpha-animation timer expired)
    Each event point calls this helper; the LAST one to arrive
    crosses the gate. Dedupes via bin.teardown_scheduled."""
    out = slot_state.get("outgoing")
    if out is None:
        return False
    if not out.eos_dispatched:
        return False
    if wipe_state["phase"] != "HOLD":
        return False
    if not wipe_state["post_wipe_frame_emitted"]:
        return False
    if out.teardown_scheduled:
        return False
    out.teardown_scheduled = True
    print(f"[sched] {out.label} all 3 gates passed -- "
          f"schedule teardown +{TEARDOWN_GRACE_MS}ms")

    def _deferred():
        out.teardown()
        if slot_state.get("outgoing") is out:
            slot_state["outgoing"] = None
        return False
    GLib.timeout_add(TEARDOWN_GRACE_MS, _deferred)
    return False


def finish_wipe_animation():
    """Snap pad geometry to post-wipe state + reset wipe phase.
    DOES NOT directly schedule teardown -- per S1, teardown waits
    for the NEXT kmssink-sink buffer to actually paint (proves
    final composited frame emitted, not just that the alpha-
    animation timer expired). The screen counter probe sets
    wipe_state.post_wipe_frame_emitted on that buffer and calls
    try_schedule_teardown_for_outgoing."""
    in_idx = wipe_state["incoming_pad_idx"]
    out_idx = wipe_state["outgoing_pad_idx"]
    if in_idx >= 0:
        set_geom(pads[in_idx], 0, DW)
    if out_idx >= 0:
        set_geom(pads[out_idx], 0, 0)
    wipe_state["phase"] = "HOLD"
    print(f"[sched] HOLD (wipe alpha-timer done, in_idx={in_idx} "
          "-- waiting on post-wipe kmssink frame for teardown gate)")
    # Try anyway: if a frame happens to have painted in the same
    # tick that the timer expired, the screen probe may already
    # have set the flag.
    try_schedule_teardown_for_outgoing()


def animation_tick():
    if wipe_state["phase"] != "WIPE":
        return True
    elapsed_s = (now_ns() - wipe_state["start_ns"]) / 1e9
    if elapsed_s >= WIPE_S:
        finish_wipe_animation()
    else:
        in_idx = wipe_state["incoming_pad_idx"]
        set_geom(pads[in_idx], 0, (elapsed_s / WIPE_S) * DW)
    return True


# --- Frame-flow instrument ---------------------------------------------

# Probes attach to the COMPOSITOR pipeline (stable, never re-built).
# Per-bin probes are not used for fps counting because bins come and
# go. The pipe_c-side probes count what reaches the compositor.
counters = {"inA": 0, "inB": 0, "screen": 0}
gap_state = {
    "inA": {"last_ns": 0, "max_gap_ms": 0.0},
    "inB": {"last_ns": 0, "max_gap_ms": 0.0},
    "screen": {"last_ns": 0, "max_gap_ms": 0.0},
}

# Per-SLOT decoded-frame counter (per QA diagnostic ask): a BUFFER
# probe on each ClipBin's v4l2h264dec src pad increments the slot's
# counter so we can distinguish "decoder never driven, only
# intervideosrc fallback feeds compositor" (decA=0 inA=30) from
# "frames flow normally + bug is elsewhere" (decA~24-30). Two bins
# on the same slot cannot be alive simultaneously (slot has one
# channel), so per-second slot-attribution is unambiguous. During a
# crossfade BOTH slots have active bins -> both counters rise.
dec_counters = {"A": 0, "B": 0}
dec_gap_state = {
    "A": {"last_ns": 0, "max_gap_ms": 0.0},
    "B": {"last_ns": 0, "max_gap_ms": 0.0},
}

try:
    # G7 (per QA dispatch): TRUNCATE on open so each script run is
    # self-contained -- QA does not have to disambiguate the new
    # soak from journal carry-over of a prior run.
    fps_log_file = open("/tmp/wipe_fps.log", "w", buffering=1)
    fps_log_file.write(f"# wipe_cpu run start; pid={os.getpid()}\n")
    print("[wipe_cpu] tee fps to /tmp/wipe_fps.log (truncated on open)")
except OSError as _exc:
    fps_log_file = None
    print(f"[wipe_cpu] /tmp/wipe_fps.log unavailable ({_exc}); "
          "stderr only")


def _make_counter_probe(key):
    s = gap_state[key]

    def _probe(_pad, _info):
        counters[key] += 1
        now_ns_local = time.monotonic_ns()
        last = s["last_ns"]
        if last:
            gap_ms = (now_ns_local - last) / 1e6
            if gap_ms > s["max_gap_ms"]:
                s["max_gap_ms"] = gap_ms
        s["last_ns"] = now_ns_local
        # S1 trigger: when this is the SCREEN (kmssink.sink) probe
        # AND a wipe just transitioned to HOLD without yet seeing a
        # post-wipe frame, mark the gate satisfied + ask the
        # scheduler to recheck the teardown triple-gate. Streaming
        # thread -> main-thread hand-off via idle_add (NEVER
        # call set_state from a streaming/probe context).
        if (key == "screen"
                and not wipe_state["post_wipe_frame_emitted"]
                and wipe_state["phase"] == "HOLD"):
            wipe_state["post_wipe_frame_emitted"] = True
            GLib.idle_add(try_schedule_teardown_for_outgoing)
        return Gst.PadProbeReturn.OK
    return _probe


def attach_flow_probes():
    for label, element, padname in [
        ("inA", srcA, "src"),
        ("inB", srcB, "src"),
        ("screen", sink, "sink"),
    ]:
        pad = element.get_static_pad(padname)
        if pad is None:
            die(f"{label}: element has no {padname} pad")
        pad.add_probe(Gst.PadProbeType.BUFFER,
                      _make_counter_probe(label))


_t_start = time.monotonic()


def fps_tick():
    uptime = int(time.monotonic() - _t_start)
    main_bin = slot_state.get("main")
    next_bin = slot_state.get("next")
    main_label = main_bin.label if main_bin else "-"
    next_label = next_bin.label if next_bin else "-"
    if main_bin is not None:
        pos_ns = main_bin.query_position_ns()
        pos_s = f"{pos_ns / 1e9:.2f}" if pos_ns >= 0 else "?"
    else:
        pos_s = "?"
    gaps = {k: int(s["max_gap_ms"]) for k, s in gap_state.items()}
    dec_gaps = {k: int(s["max_gap_ms"]) for k, s in dec_gap_state.items()}
    line = (
        f"[fps] t={uptime} "
        f"inA={counters['inA']}(g{gaps['inA']}) "
        f"inB={counters['inB']}(g{gaps['inB']}) "
        f"screen={counters['screen']}(g{gaps['screen']}) "
        f"decA={dec_counters['A']}(g{dec_gaps['A']}) "
        f"decB={dec_counters['B']}(g{dec_gaps['B']}) "
        f"main={main_label} next={next_label} "
        f"pts={pos_s} state={wipe_state['phase']}"
    )
    print(line, file=sys.stderr, flush=True)
    if fps_log_file is not None:
        try:
            fps_log_file.write(line + "\n")
        except OSError:
            pass
    counters["inA"] = 0
    counters["inB"] = 0
    counters["screen"] = 0
    dec_counters["A"] = 0
    dec_counters["B"] = 0
    for s in gap_state.values():
        s["max_gap_ms"] = 0.0
    for s in dec_gap_state.values():
        s["max_gap_ms"] = 0.0
    return True


# --- Compositor bus + signals -----------------------------------------

loop = GLib.MainLoop()


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
        print("[comp] pipeline EOS (decoder-isolated bridge "
              "should have prevented this)", file=sys.stderr)
        loop.quit()


bus = pipe_c.get_bus()
bus.add_signal_watch()
bus.connect("message", on_comp_bus)


shutdown_requested = False


def shutdown(*_):
    global shutdown_requested
    shutdown_requested = True
    GLib.idle_add(loop.quit)


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)


# --- Compositor preroll helper ----------------------------------------

def preroll_to_paused(p, label):
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
        r, cur, pending = p.get_state(1 * Gst.SECOND)
        if r == Gst.StateChangeReturn.SUCCESS:
            print(f"[wipe_cpu] {label} preroll done after ~{elapsed}s "
                  f"state={cur.value_nick}")
            return True
        if r == Gst.StateChangeReturn.NO_PREROLL:
            print(f"[wipe_cpu] {label} reached PAUSED after "
                  f"~{elapsed}s (NO_PREROLL)")
            return True
        if r == Gst.StateChangeReturn.FAILURE:
            die(f"{label} preroll FAILURE")
        print(f"[wipe_cpu] {label} preroll... state={cur.value_nick} "
              f"pending={pending.value_nick} "
              f"({elapsed}/{PREROLL_BUDGET_S}s)")
    die(f"{label} preroll exceeded {PREROLL_BUDGET_S}s budget")


# --- Run ---------------------------------------------------------------

# RUN-START marker per QA dispatch: a unique single-token line at
# startup so a run-scoped log capture (/tmp/wipe_run.log) is
# unambiguously bracketed and grep-able from journal carry-over.
print("==RUN-START== wipe_cpu", file=sys.stderr)
print(f"[wipe_cpu] starting; WIPE={WIPE_S}s "
      f"display={DW}x{DH}@{FPS} fresh-bin-per-clip; ctrl-c to stop",
      file=sys.stderr)
print(f"[wipe_cpu] leak-fallback videoconvert before intervideosink: "
      f"{'ON' if INSERT_TEARDOWN_VIDEOCONVERT else 'off'} "
      "(env OPENMARQUEE_VIDEOCONVERT_BEFORE_INTERVIDEOSINK)")

# Compositor pipeline first (live sources -> NO_PREROLL accepted).
preroll_to_paused(pipe_c, "compositor")

if shutdown_requested:
    pipe_c.set_state(Gst.State.NULL)
    sys.exit(0)

# Capture shared base-time AFTER compositor is in PAUSED, BEFORE
# anything else goes PLAYING. Every fresh ClipBin pins this same
# base-time so PTS is comparable across pipelines.
shared_base_time = shared_clock.get_time()
print(f"[wipe_cpu] shared clock={shared_clock.get_name()} "
      f"base_time={shared_base_time} ns")
pipe_c.set_base_time(shared_base_time)

# Initial bin: spawn slot A, preroll, then play. Trigger probe +
# duration wire themselves via pad-added on the demuxer when
# qtdemux exposes its video pad (within ms of PAUSED). No
# synchronous post-preroll work that would race the dynamic pad.
print("[wipe_cpu] spawn initial main bin (slot A)")
first = ClipBin("A").build()
first.preroll()
slot_state["main"] = first

# Start compositor PLAYING.
if pipe_c.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
    die("compositor: pipeline failed to enter PLAYING")

# Set initial pad geometry: slot 0 (A) full, slot 1 (B) hidden.
set_geom(pads[0], 0, DW)
set_geom(pads[1], 0, 0)
pads[0].set_property("zorder", 0)
pads[1].set_property("zorder", 1)

# Start first bin PLAYING.
first.play()

attach_flow_probes()
GLib.timeout_add(TICK_MS, animation_tick)
GLib.timeout_add(1000, fps_tick)
try:
    loop.run()
finally:
    print("[wipe_cpu] shutdown -> teardown all bins + comp")
    for key in ("main", "next", "outgoing"):
        b = slot_state.get(key)
        if b is not None:
            b.teardown()
            slot_state[key] = None
    pipe_c.set_state(Gst.State.NULL)
    if fps_log_file is not None:
        try:
            fps_log_file.close()
        except OSError:
            pass
