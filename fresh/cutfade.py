#!/usr/bin/env python3
"""fresh/cutfade.py -- continuous 1s GL crossfade reel across all clips.

Per qarl + QA dispatch 2026-06-18: revive the 1s crossfade in the
NEW Python clean-room renderer. NOT the old Rust renderer; NOT
modifying cutloop.py. This file IS the cycling crossfade reel.

QA on-glass verified bc0cbcc POC: glimagesink (GL->EGL->KMS direct,
NO gldownload/videoconvert) prerolled + ran a clean 1s A->B fade at
~24fps (screen steady 24, max_gap 43-53ms during the fade = smooth).
GL crossfade architecture + the 24fps path are PROVEN on this Pi.

THIS COMMIT extends the one-shot POC into a continuous cycling reel:
  - glimagesink is the DEFAULT sink (the proven 24fps path).
  - Dynamic glob discovery of every asset.mp4 under
    /var/openmarquee/content/<uuid>/ (sorted by path; same as
    cutloop's discovery).
  - Two-slot state machine on mix.sink_0 / mix.sink_1. At any
    moment one slot is the CURRENT (alpha=1) and the other is
    either empty or being PRIMED for the next crossfade.
  - On each clip cycle: prime the next clip into the OFF slot
    PRIME_LEAD_S=1.5s before its currently-active clip ends, gate
    on the incoming decoder's first frame (or deadline fallback),
    fade alpha 1<->0 over FADE_S=1.0s, swap which slot is current,
    retire the (now-outgoing) slot with the closure-disconnect
    pattern from cutloop's retire-leak fix.
  - WATCHDOG: 1Hz check, if screen frames = 0 for 2 consecutive
    seconds, force-prime + force-PLAY.
  - Pools MODEST per QA: no max-size-buffers boost.
  - Concurrent decoder count capped at 2 (one current + one
    being-primed) -- intrinsic to the two-slot design.

ARCHITECTURE:

  Slot 0:                            mix.sink_0 (alpha animated)
    Gst.Bin "slot_0":                    |
      filesrc -> qtdemux ----- video --> h264parse(cfg=-1)
        |                                  |
        +--<pad-added>--> link             v4l2h264dec
                                           |
                                           glupload
                                           |
                                           ghost src ----------|
                                                               v
  Slot 1:                            mix.sink_1 (alpha animated)
    same structure
                                                               |
                                  glvideomixer name=mix <------+
                                       |
                                       v
                                  glimagesink sync=true
                                  (or gldownload->videoconvert->
                                  kmssink fallback via env flag)

LOOP MECHANISM:
  - Each new slot (sub-bin) gets:
      stored pad_added_handler_id from qtdemux.connect.
      stored first_frame_probe_id from dec.src.add_probe.
      stored prime_trigger_probe_id from qtdemux video src.add_probe.
  - Retire releases ALL of those plus mix.release_request_pad
    plus sub.set_state(NULL) plus pipeline.remove (cutloop's
    proven sequence).
  - Prime trigger BUFFER probe on the CURRENT slot's qtdemux video
    src pad. When pts >= (dur - PRIME_LEAD_S - FADE_S), idle_add
    the prime; one-shot via REMOVE.
  - Gate BUFFER probe on the OFF slot's v4l2h264dec src pad. When
    first buffer arrives, schedule start_fade; one-shot via REMOVE.
  - Deadline timer: if gate doesn't fire within GATE_DEADLINE_MS,
    start_fade anyway (hold-last-frame semantics of glvideomixer).
  - fade_tick animates alphas over FADE_S; on completion swaps
    current slot index, schedules retire of old slot, attaches a
    new prime trigger on the new current slot's qtdemux pad.

WATCHDOG: per QA spec. fps_tick reads screen_state.frames; if 0
for 2 consecutive ticks (= 2s of black), force-prime the OFF slot
on the next clip in cycle order + force the pipeline to PLAYING.
"""

import gc
import glob
import os
import signal
import subprocess
import sys
import time
import weakref

# GL env vars MUST be set before Gst.init so gstreamer-gl picks the
# headless GBM/EGL backend on dri/card0 (no X/Wayland needed).
os.environ.setdefault("GST_GL_PLATFORM", "egl")
os.environ.setdefault("GST_GL_WINDOW", "gbm")
os.environ.setdefault("GST_GL_API", "gles2")


def _ensure_xdg_runtime_dir():
    """systemd transient units launched without logind get no
    XDG_RUNTIME_DIR. Mesa vc4 EGL/GBM uses it for the dri socket
    path and the shader cache discovery; without it the first GL
    context cold-compiles every shader -> PAUSED preroll past 10s."""
    if os.environ.get("XDG_RUNTIME_DIR"):
        return
    uid = os.getuid()
    for candidate in (f"/run/user/{uid}", f"/tmp/runtime-{uid}"):
        try:
            os.makedirs(candidate, mode=0o700, exist_ok=True)
            os.chmod(candidate, 0o700)
            os.environ["XDG_RUNTIME_DIR"] = candidate
            print(f"[cutfade] XDG_RUNTIME_DIR fallback: {candidate}",
                  file=sys.stderr)
            return
        except OSError:
            continue


_ensure_xdg_runtime_dir()

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gst  # noqa: E402


# --- Config -------------------------------------------------------------

CONTENT_GLOB = "/var/openmarquee/content/*/asset.mp4"
VIDEOS = sorted(glob.glob(CONTENT_GLOB))

PRIME_LEAD_S = 1.5     # seconds before current EOS to spawn next
FADE_S = 1.0           # crossfade duration
FADE_TICK_MS = 33      # animation tick (~30fps geometry update)
GATE_DEADLINE_MS = 2000  # max wait for incoming first frame
PREROLL_BUDGET_S = 30  # GL cold-start budget under systemd
WATCHDOG_STALL_S = 2   # consecutive seconds of screen=0 -> recover
# Per QA d489fd8 soak: repeat-after-eos can freeze a composite
# at 24fps so screen=24 is no longer proof of motion. Detect
# "no decode-PTS progress on the current slot" as a third-line
# defense behind the pts-threshold trigger and the EOS-backup
# trigger. Threshold low enough to catch a freeze quickly, high
# enough not to trip on a slow-decode hiccup or natural
# end-of-clip + fade lag.
FROZEN_STALL_S = 3     # consecutive seconds of unchanged dec PTS

# Fallback durations if qtdemux query_duration fails. Per QA assets
# are uniform h264 Main 1280x720 24fps but vary in length; if a
# specific asset's query fails the fallback is ~6s (rough average).
DUR_FALLBACK_NS = 6_000_000_000

# Default to glimagesink per QA glass-verified 24fps path. Set
# OPENMARQUEE_CUTFADE_SINK=kmssink to use the gldownload+
# videoconvert+kmssink fallback path (the wipe_cpu.py path; runs
# ~1-7fps so only useful for verification).
SINK_CHOICE = os.environ.get(
    "OPENMARQUEE_CUTFADE_SINK", "glimagesink"
).lower()


def die(msg, code=1):
    print(f"[cutfade] {msg}", file=sys.stderr)
    sys.exit(code)


# --- Pre-flights --------------------------------------------------------

if not VIDEOS:
    die(f"no videos discovered under {CONTENT_GLOB} -- "
        "check /var/openmarquee/content exists + contains "
        "<uuid>/asset.mp4 files")
for v in VIDEOS:
    if not os.path.exists(v):
        die(f"missing video: {v}")
print(f"[cutfade] discovered {len(VIDEOS)} videos:", file=sys.stderr)
for v in VIDEOS:
    print(f"[cutfade]   {os.path.basename(os.path.dirname(v))}",
          file=sys.stderr)

Gst.init(None)

REQUIRED_ELEMENTS = ("filesrc", "qtdemux", "h264parse", "v4l2h264dec",
                     "glupload", "glvideomixer")
if SINK_CHOICE == "kmssink":
    REQUIRED_ELEMENTS += ("gldownload", "videoconvert", "kmssink")
else:
    REQUIRED_ELEMENTS += ("glimagesink",)

for el in REQUIRED_ELEMENTS:
    if not Gst.ElementFactory.find(el):
        die(f"missing gstreamer element: {el}")

self_pid = os.getpid()
parent_pid = os.getppid()
own_pids = (str(self_pid), str(parent_pid))
ps = subprocess.run(
    ["pgrep", "-af",
     "v4l2h264dec|openmarquee-render|mini-play|wipe.py|wipe_cpu|"
     "cutloop|cutfade"],
    capture_output=True, text=True,
)
peers = [
    line for line in ps.stdout.splitlines()
    if line.split() and line.split()[0] not in own_pids
]
if peers:
    die("another HW-decode process is already running:\n  "
        + "\n  ".join(peers))


# --- Pipeline -----------------------------------------------------------

pipeline = Gst.Pipeline.new("cutfade")
if pipeline is None:
    die("Gst.Pipeline.new returned None")

mix = Gst.ElementFactory.make("glvideomixer", "mix")
mix.set_property("background", 1)  # black

if SINK_CHOICE == "glimagesink":
    sink = Gst.ElementFactory.make("glimagesink", "sink")
    sink.set_property("sync", True)
    pipeline.add(mix)
    pipeline.add(sink)
    if not mix.link(sink):
        die("link mix -> glimagesink failed")
    print("[cutfade] sink path: glvideomixer -> glimagesink",
          file=sys.stderr)
else:
    gldownload = Gst.ElementFactory.make("gldownload", "gldl")
    videoconvert = Gst.ElementFactory.make("videoconvert", "conv")
    sink = Gst.ElementFactory.make("kmssink", "sink")
    sink.set_property("sync", True)
    for el in (mix, gldownload, videoconvert, sink):
        pipeline.add(el)
    if not mix.link(gldownload):
        die("link mix -> gldownload failed")
    if not gldownload.link(videoconvert):
        die("link gldownload -> videoconvert failed")
    if not videoconvert.link(sink):
        die("link videoconvert -> kmssink failed")
    print("[cutfade] sink path: glvideomixer -> gldownload -> "
          "videoconvert -> kmssink (fallback)", file=sys.stderr)

# Define loop here so any code path can call loop.quit() safely
# (before loop.run() it's a no-op; after, triggers finally cleanup).
loop = GLib.MainLoop()


# --- Lazy mix sink pad allocation --------------------------------------
#
# Per QA's 5-subagent + adversarial-verifier design pass on the
# 559897f soak result. Three pad-lifecycle strategies have been
# proven bad on glass against GstVideoAggregator:
#
#   00ca09e: release_request_pad mid-stream -> aggregator output
#            stops -> black screen after first retire.
#   1dcf4f4: pre-allocate both pads, both unfed at startup ->
#            preroll blocks 30s on the unfed pad -> timeout.
#   559897f: lazy alloc + keep pad-after-retire + EOS-DROP probe
#            -> kept pad never delivers a buffer and never goes
#            EOS, so aggregator waits forever each tick -> stall.
#
# Root: GstVideoAggregator's aggregate tick blocks until every
# non-EOS sink pad has a buffer ready. The EOS-DROP probe of
# 559897f locked the kept pad out of that condition.
#
# Variant-iii fix (verifier's safest pick):
#   - Lazy-allocate mix_pads[slot_idx] on first use (preroll
#     stays fixed -- only the slot 0 pad exists at PAUSED).
#   - Set repeat-after-eos=True on each pad at allocation
#     (GstGLVideoMixerInput in 1.26 has this; glass-confirmed
#     on fireplacesign). Source: an EOS pad with this property
#     freezes its last frame AND forces pipeline-eos to stay
#     FALSE whenever any repeat pad is involved -- a single
#     source EOS can NEVER tear down the pipeline. This replaces
#     every reason the EOS-DROP probe existed.
#   - Retire SENDS EOS directly to the kept mix sink pad
#     (send_event is synchronous on the aggregator sink-event
#     handler, so priv->eos = True by return). This ENGAGES the
#     repeat-after-eos behavior: without an EOS the property
#     does nothing -- sub.set_state(NULL) alone does NOT emit
#     EOS downstream. After the EOS the aggregator freezes the
#     pad's last buffer and subsequent ticks treat it as
#     "has a buffer" so the live pad keeps compositing at
#     24fps. Then retire sets alpha=0 on the kept pad, so
#     gst_gl_video_mixer_process_textures skips it entirely
#     (continue on alpha==0): zero visual cost, zero stall.
#   - On re-prime, FLUSH_START + FLUSH_STOP(reset_time=False) on
#     the kept mix sink pad clears priv->eos (and the sticky
#     EOS event) and re-inits ONLY this aggpad segment (per
#     aggregator_pad_reset_unlocked). reset_time=False keeps the
#     srcpad/output running-time stable -- the live pad sees no
#     clock jump. Pad goes live again with the new branch.
#
# NEVER release_request_pad on a glvideomixer sink pad
# mid-stream (proven on 00ca09e to stop aggregator output).
mix_pads = [None, None]


def _ensure_mix_pad(slot_idx):
    """Lazy-allocate mix_pads[slot_idx] on first use; set
    repeat-after-eos=True so a per-source EOS does NOT tear down
    the pipeline AND the kept pad never stalls the aggregator on
    later ticks (its frozen last frame is skipped by alpha=0).
    Returns the persistent pad. Subsequent calls for the same
    slot return the existing pad."""
    if mix_pads[slot_idx] is None:
        p = mix.request_pad_simple("sink_%u")
        if p is None:
            die(f"mix.request_pad_simple failed for slot {slot_idx}")
        p.set_property("alpha", 0.0)
        p.set_property("zorder", 0)
        p.set_property("repeat-after-eos", True)
        mix_pads[slot_idx] = p
        print(f"[cutfade] lazy-allocated mix.sink_{slot_idx} "
              "+ repeat-after-eos=True (first build for slot)",
              file=sys.stderr)
    return mix_pads[slot_idx]


# Per-slot id of the current first-buffers instrument probe. If a
# prior re-prime's instrument never collected 3 buffers (e.g. new
# clip dropped before any buffer crossed mix.sink), the probe and
# its closure refs would accumulate across cycles. We track the
# id here and remove any stale instrument before attaching a
# fresh one.
_reprime_probe_ids = [None, None]


def _attach_first_buffers_log(slot_idx, mix_sink, slot_label):
    """Log (buf pts, segment running-time) for the FIRST 3 buffers
    crossing this mix sink pad after a re-prime. Per QA: catches
    a backwards or discontinuous running-time at a swap. Probe
    self-removes after 3 buffers; also removed defensively on the
    next re-prime of this slot if it never reached 3 (no buffers
    delivered before retire)."""
    if _reprime_probe_ids[slot_idx] is not None:
        try:
            mix_sink.remove_probe(_reprime_probe_ids[slot_idx])
        except Exception as exc:
            print(f"[cutfade] [reprime-buf] stale probe remove "
                  f"WARN slot {slot_idx}: {exc}", file=sys.stderr)
        _reprime_probe_ids[slot_idx] = None

    counter = [0]

    def _on_buf(_pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        pts = buf.pts
        rt = Gst.CLOCK_TIME_NONE
        try:
            seg_event = mix_sink.get_sticky_event(
                Gst.EventType.SEGMENT, 0
            )
            if seg_event is not None:
                seg = seg_event.parse_segment()
                if (seg is not None
                        and pts != Gst.CLOCK_TIME_NONE):
                    rt = seg.to_running_time(
                        Gst.Format.TIME, pts
                    )
        except Exception as exc:
            print(f"[cutfade] [reprime-buf] seg parse WARN: "
                  f"{exc}", file=sys.stderr)
        counter[0] += 1
        pts_ms = (pts / 1e6
                  if pts != Gst.CLOCK_TIME_NONE else -1.0)
        rt_ms = (rt / 1e6
                 if rt != Gst.CLOCK_TIME_NONE else -1.0)
        print(f"[cutfade] [reprime-buf] {slot_label} "
              f"buf#{counter[0]} pts_ms={pts_ms:.1f} "
              f"rt_ms={rt_ms:.1f}", file=sys.stderr)
        if counter[0] >= 3:
            _reprime_probe_ids[slot_idx] = None
            return Gst.PadProbeReturn.REMOVE
        return Gst.PadProbeReturn.OK

    probe_id = mix_sink.add_probe(
        Gst.PadProbeType.BUFFER, _on_buf
    )
    _reprime_probe_ids[slot_idx] = probe_id


# --- Slot state machine -------------------------------------------------

# Two slots, each can hold a sub-bin feeding mix.sink_0 / sink_1.
# At any moment one is the CURRENT (alpha=1) and the other is
# either empty (no sub-bin) or being PRIMED (alpha=0, pre-fade).
slots = [
    {"sub": None, "dec": None, "demux": None, "mix_sink": None,
     "label": None, "serial": None, "dur_ns": 0,
     "pad_added_id": None, "first_frame_probe_id": None,
     "prime_trigger_probe_id": None, "prime_eos_probe_id": None,
     "dec_pts_probe_id": None, "last_dec_pts_ns": 0},
    {"sub": None, "dec": None, "demux": None, "mix_sink": None,
     "label": None, "serial": None, "dur_ns": 0,
     "pad_added_id": None, "first_frame_probe_id": None,
     "prime_trigger_probe_id": None, "prime_eos_probe_id": None,
     "dec_pts_probe_id": None, "last_dec_pts_ns": 0},
]
current_slot_idx = [0]   # which slots[] entry is the active one
next_clip_idx = [0]       # cycling counter through VIDEOS

# Fade state
fade_state = {"start_ns": 0, "in_flight": False,
              "incoming_slot": -1, "outgoing_slot": -1}

# Per-slot "this slot's succession is already scheduled" flag. Set
# by ANY trigger that calls prime_next (pts-threshold trigger, EOS
# backup trigger, FROZEN watchdog). Cleared in finish_fade for the
# new current slot (fresh) and in retire_slot defensively.
# Prevents double-prime when two triggers race on the same slot.
advance_scheduled = [False, False]

# Per QA d489fd8+5ec8044 soak: a prime_next that hits a fade
# in-flight USED to be dropped (logged + returned False). The
# log message promised "next prime will fire on the new current"
# but nothing actually re-fired it. On short clips this stranded
# the reel until FROZEN WATCHDOG force-advanced 3s later (visible
# freeze + churn). Fix: defer the dropped prime onto this flag;
# finish_fade re-fires it for the new current after the swap.
# Module-level singleton -- prime_next is the only writer when
# deferring; finish_fade clears and re-schedules.
pending_prime = [False]

# Watchdog state. zero_frame_seconds: legacy screen=0 counter.
# last_dec_pts_ns + frozen_seconds: FROZEN-detection on the current
# slot (catches "screen=24 of frozen composite" per QA d489fd8).
watchdog_state = {"zero_frame_seconds": 0,
                  "last_dec_pts_ns": 0,
                  "frozen_seconds": 0}

# Cycle serial (every spawned bin gets a unique id for log clarity)
spawn_serial = [0]


def _slot_clear(slot):
    """Reset slot dict to "empty" state (does not touch gst -- caller
    must have already retired the elements)."""
    for k in ("sub", "dec", "demux", "mix_sink", "label", "serial",
              "pad_added_id", "first_frame_probe_id",
              "prime_trigger_probe_id", "prime_eos_probe_id",
              "dec_pts_probe_id"):
        slot[k] = None
    slot["dur_ns"] = 0
    slot["last_dec_pts_ns"] = 0


def build_slot(slot_idx, clip_idx):
    """Build a sub-bin for VIDEOS[clip_idx] into slots[slot_idx].
    Returns (sub_bin, mix_sink_pad, decoder, demuxer, ids...).
    Caller is responsible for setting alpha + state. Stores all
    teardown-required IDs on the slot dict so retire_slot can
    disconnect/remove them all."""
    asset = VIDEOS[clip_idx]
    spawn_serial[0] += 1
    serial = spawn_serial[0]
    label = f"clip{serial}"
    asset_name = os.path.basename(os.path.dirname(asset)) or asset

    sub = Gst.Bin.new(f"slot_{slot_idx}_{label}")
    filesrc = Gst.ElementFactory.make("filesrc", None)
    filesrc.set_property("location", asset)
    qtdemux = Gst.ElementFactory.make("qtdemux", None)
    h264parse = Gst.ElementFactory.make("h264parse", None)
    h264parse.set_property("config-interval", -1)
    decoder = Gst.ElementFactory.make("v4l2h264dec", f"dec_{label}")
    glupload = Gst.ElementFactory.make("glupload", None)
    for el in (filesrc, qtdemux, h264parse, decoder, glupload):
        if el is None:
            die(f"[{label}] factory.make returned None")
        sub.add(el)

    if not filesrc.link(qtdemux):
        die(f"[{label}] filesrc -> qtdemux link failed")

    def _pad_added(_demux, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        caps_str = caps.to_string() if caps else ""
        if not caps_str.startswith("video/"):
            return
        sink_pad = h264parse.get_static_pad("sink")
        if sink_pad and not sink_pad.is_linked():
            pad.link(sink_pad)
    pad_added_id = qtdemux.connect("pad-added", _pad_added)

    if not h264parse.link(decoder):
        die(f"[{label}] h264parse -> v4l2h264dec link failed")
    if not decoder.link(glupload):
        die(f"[{label}] v4l2h264dec -> glupload link failed")

    glupload_src = glupload.get_static_pad("src")
    ghost = Gst.GhostPad.new("src", glupload_src)
    sub.add_pad(ghost)

    pipeline.add(sub)
    # Per QA variant-iii fix: mix sink pads are PERSISTENT.
    # _ensure_mix_pad lazy-allocates on first use and sets
    # repeat-after-eos=True so a retired pad's upstream EOS
    # neither cascades to pipeline EOS nor stalls the aggregator
    # (the frozen-last-frame is skipped by alpha=0). On a re-prime
    # build for this slot we FLUSH the kept pad before relinking:
    # FLUSH_STOP(reset_time=False) clears priv->eos and re-inits
    # ONLY this aggpad's segment (srcpad/output untouched, no
    # clock jump on the live pad). Belt-and-suspenders with
    # repeat-after-eos: removes any relink race where a buffer
    # arrives before the aggregator has processed the segment.
    is_reprime = mix_pads[slot_idx] is not None
    mix_sink = _ensure_mix_pad(slot_idx)
    # Defensive: if a prior retire missed an unlink, clear it.
    prior_peer = mix_sink.get_peer()
    if prior_peer is not None:
        print(f"[cutfade] WARN mix.sink_{slot_idx} had stale peer "
              "at build; unlinking before relink", file=sys.stderr)
        prior_peer.unlink(mix_sink)
    if is_reprime:
        print(f"[cutfade] re-prime slot {slot_idx}: FLUSH "
              "kept mix.sink before relink", file=sys.stderr)
        mix_sink.send_event(Gst.Event.new_flush_start())
        mix_sink.send_event(Gst.Event.new_flush_stop(False))
        _attach_first_buffers_log(slot_idx, mix_sink, label)
    if ghost.link(mix_sink) != Gst.PadLinkReturn.OK:
        die(f"[{label}] ghost -> mix.sink link failed")

    slot = slots[slot_idx]
    slot["sub"] = sub
    slot["dec"] = decoder
    slot["demux"] = qtdemux
    slot["mix_sink"] = mix_sink
    slot["label"] = label
    slot["serial"] = serial
    slot["pad_added_id"] = pad_added_id
    slot["last_dec_pts_ns"] = 0

    # Per QA d489fd8 ask #3: FROZEN watchdog needs per-slot
    # decoder-PTS visibility. Attach a BUFFER probe on dec.src
    # that updates slot["last_dec_pts_ns"] on every decoded
    # frame. Cheap (one int store per 24fps), lifelong (removed
    # in retire_slot). Used by fps_tick to detect "screen=24 but
    # the composite is a frozen last frame" and force-advance.
    dec_src_for_pts = decoder.get_static_pad("src")
    if dec_src_for_pts is not None:
        def _on_dec_buf(_pad, info, _slot=slot):
            buf = info.get_buffer()
            if buf is not None and buf.pts != Gst.CLOCK_TIME_NONE:
                _slot["last_dec_pts_ns"] = buf.pts
            return Gst.PadProbeReturn.OK
        slot["dec_pts_probe_id"] = dec_src_for_pts.add_probe(
            Gst.PadProbeType.BUFFER, _on_dec_buf
        )

    print(f"[cutfade] build slot {slot_idx}: {label} = {asset_name}",
          file=sys.stderr)
    return slot


def retire_slot(slot_idx):
    """Tear down the sub-bin in slots[slot_idx]. Uses cutloop's
    proven disconnect-before-NULL sequence to prevent closure-ref
    leaks (Python closures captured by the pad-added handler + any
    probes hold the sub-bin alive otherwise)."""
    slot = slots[slot_idx]
    sub = slot["sub"]
    if sub is None:
        return False
    label = slot["label"] or f"slot_{slot_idx}"
    weak = weakref.ref(sub)

    try:
        if slot["pad_added_id"] and slot["demux"] is not None:
            try:
                slot["demux"].disconnect(slot["pad_added_id"])
            except Exception as exc:
                print(f"[cutfade] retire {label} pad-added "
                      f"disconnect WARN: {exc}", file=sys.stderr)
        if slot["dec"] is not None:
            dec_src = slot["dec"].get_static_pad("src")
            if dec_src is not None:
                if slot["first_frame_probe_id"]:
                    try:
                        dec_src.remove_probe(
                            slot["first_frame_probe_id"]
                        )
                    except Exception as exc:
                        print(f"[cutfade] retire {label} first-frame "
                              f"probe WARN: {exc}", file=sys.stderr)
                if slot["dec_pts_probe_id"]:
                    try:
                        dec_src.remove_probe(
                            slot["dec_pts_probe_id"]
                        )
                    except Exception as exc:
                        print(f"[cutfade] retire {label} dec_pts "
                              f"probe WARN: {exc}", file=sys.stderr)
        if (slot["demux"] is not None
                and (slot["prime_trigger_probe_id"]
                     or slot["prime_eos_probe_id"])):
            # demux video src pad -- find it; same pad holds
            # both the prime-threshold BUFFER probe and the EOS
            # backup-trigger EVENT probe (attach_prime_trigger).
            it = slot["demux"].iterate_src_pads()
            while True:
                res, p = it.next()
                if res != Gst.IteratorResult.OK:
                    break
                if p.get_name().startswith("video"):
                    if slot["prime_trigger_probe_id"]:
                        try:
                            p.remove_probe(
                                slot["prime_trigger_probe_id"]
                            )
                        except Exception as exc:
                            print(f"[cutfade] retire {label} prime "
                                  f"trigger probe WARN: {exc}",
                                  file=sys.stderr)
                    if slot["prime_eos_probe_id"]:
                        try:
                            p.remove_probe(
                                slot["prime_eos_probe_id"]
                            )
                        except Exception as exc:
                            print(f"[cutfade] retire {label} prime "
                                  f"eos probe WARN: {exc}",
                                  file=sys.stderr)
                    break
        # Per QA glass diagnosis of 3d36db1: repeat-after-eos only
        # ENGAGES once the pad RECEIVES an EOS event. Setting the
        # source sub-bin to NULL does NOT emit EOS downstream to
        # the kept mix sink pad. Without an EOS the aggregator
        # still treats the kept pad as a live waiter -> blocks
        # waiting for a buffer that never arrives -> screen=0
        # (same stall shape as 559897f).
        #
        # Fix: send EOS DIRECTLY into the kept mix sink pad before
        # we unlink the peer. send_event is synchronous on the
        # sink pad's event function (gst_aggregator_default_sink
        # _event), so aggpad->priv->eos = True by the time we
        # return. With repeat-after-eos=True (set in
        # _ensure_mix_pad) the aggregator then freezes the last
        # buffer on this pad so subsequent ticks treat it as
        # "has a buffer" (composited-then-skipped on alpha==0).
        # global-eos is forced FALSE whenever any repeat pad is
        # involved -> the pipeline can NEVER tear down from this
        # single pad's EOS. The re-prime FLUSH_START + FLUSH_STOP
        # (already in build_slot for the re-prime branch) clears
        # this priv->eos when the next clip relinks -> pad goes
        # live again.
        #
        # Order: EOS first (pad object still valid + linked at
        # that point so the event routes cleanly), then peer
        # unlink, then alpha=0, then sub NULL + remove.
        # NEVER release_request_pad on a glvideomixer sink pad
        # (proven on 00ca09e to stop aggregator output cold).
        if slot["mix_sink"] is not None:
            slot["mix_sink"].send_event(Gst.Event.new_eos())
            peer = slot["mix_sink"].get_peer()
            if peer is not None:
                peer.unlink(slot["mix_sink"])
            slot["mix_sink"].set_property("alpha", 0.0)
        sub.set_state(Gst.State.NULL)
        pipeline.remove(sub)
        print(f"[cutfade] retire {label} (slot {slot_idx}; "
              "mix.sink pad kept allocated)",
              file=sys.stderr)
    except Exception as exc:
        print(f"[cutfade] retire {label} WARN: {exc}",
              file=sys.stderr)

    # Clear the per-slot succession-scheduled flag defensively.
    # finish_fade already clears the new current's flag; this
    # covers the outgoing slot so a future re-prime of THIS
    # slot starts with a clean flag.
    advance_scheduled[slot_idx] = False

    _slot_clear(slot)

    def _check_leak():
        gc.collect()
        if weak() is not None:
            print(f"[cutfade] [retire] LEAK ref still held for "
                  f"{label}", file=sys.stderr)
        return False
    GLib.idle_add(_check_leak)
    return False


# --- Prime trigger via PTS probe on demuxer src ------------------------

def attach_prime_trigger(slot_idx):
    """Attach a BUFFER probe on the slot's demuxer video src pad.
    When pts >= dur - (PRIME_LEAD_S + FADE_S), schedule prime_next
    via GLib.idle_add and remove the probe. dur is queried from
    the demuxer."""
    slot = slots[slot_idx]
    if slot["demux"] is None:
        return
    # qtdemux is dynamic; the video src pad may not exist yet at
    # the moment of attach. Defer until pad-added fires (which we
    # already hooked). Use iterate when ready -- if the video pad
    # exists now, attach immediately; otherwise reschedule.
    it = slot["demux"].iterate_src_pads()
    video_pad = None
    while True:
        res, p = it.next()
        if res != Gst.IteratorResult.OK:
            break
        if p.get_name().startswith("video"):
            video_pad = p
            break
    if video_pad is None:
        # Pad not yet available; retry shortly. qtdemux exposes its
        # src pads within ms of PAUSED.
        GLib.timeout_add(50, lambda: attach_prime_trigger(slot_idx)
                         or False)
        return False

    # Query duration on the demuxer. Per cutloop pattern, this
    # works once the moov box is parsed (which is what triggers
    # pad-added). Per QA d489fd8: log WIN/FALLBACK so the next
    # soak can confirm or kill the "fallback duration overshoots
    # real EOS -> trigger misses" hypothesis.
    ok, dur = slot["demux"].query_duration(Gst.Format.TIME)
    if ok and dur > 0:
        slot["dur_ns"] = dur
        dur_source = "WIN"
    else:
        slot["dur_ns"] = DUR_FALLBACK_NS
        dur_source = "FALLBACK"
        print(f"[cutfade] {slot['label']} duration query failed; "
              f"fallback {DUR_FALLBACK_NS / 1e9:.2f}s",
              file=sys.stderr)
    prime_at = slot["dur_ns"] - int(
        (PRIME_LEAD_S + FADE_S) * 1_000_000_000
    )
    print(f"[cutfade] [trigger] slot={slot_idx} "
          f"{dur_source} dur_ns={slot['dur_ns']} "
          f"threshold_ns={prime_at} "
          f"({slot['dur_ns'] / 1e9:.2f}s -> "
          f"fire at {prime_at / 1e9:.2f}s)",
          file=sys.stderr)
    if prime_at <= 0:
        # Clip too short for prime-lead arithmetic; skip the
        # pts-threshold trigger and rely on the EOS backup
        # trigger below to advance at natural EOS. Should not
        # happen for our 5-9s assets but defensive.
        print(f"[cutfade] {slot['label']} dur too short for "
              f"prime arithmetic ({slot['dur_ns'] / 1e9:.2f}s); "
              "relying on EOS backup trigger", file=sys.stderr)

    fired = [False]

    def _on_buffer(_pad, info):
        if fired[0]:
            return Gst.PadProbeReturn.REMOVE
        buf = info.get_buffer()
        if buf is None or buf.pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK
        if prime_at > 0 and buf.pts >= prime_at:
            fired[0] = True
            print(f"[cutfade] {slot['label']} prime_trigger FIRED "
                  f"at pts={buf.pts / 1e9:.2f}s "
                  f"(threshold={prime_at / 1e9:.2f}s)",
                  file=sys.stderr)
            advance_scheduled[slot_idx] = True
            GLib.idle_add(prime_next)
            # Self-clear so retire does not double-remove (gst
            # warns "pad has no probe with id N").
            slot["prime_trigger_probe_id"] = None
            return Gst.PadProbeReturn.REMOVE
        return Gst.PadProbeReturn.OK

    # Per QA d489fd8 ask #2: EOS-driven BACKUP trigger. If the
    # pts-threshold buffer probe missed (e.g. fallback duration
    # too long), the clip will naturally EOS before fired[0]
    # ever flips. Catch the EOS on the same demux video src pad
    # and drive prime_next + start_fade if no advance is
    # already scheduled. Guards: not in fade, this slot IS the
    # current visible slot, and not already advance-scheduled.
    # This is the safety net QA asked for -- "a cut-style
    # advance as the safety net beats a frozen reel."
    def _on_event(_pad, info):
        ev = info.get_event()
        if ev is None or ev.type != Gst.EventType.EOS:
            return Gst.PadProbeReturn.OK
        if fired[0]:
            print(f"[cutfade] {slot['label']} EOS after "
                  f"prime_trigger (normal end)", file=sys.stderr)
            return Gst.PadProbeReturn.OK
        print(f"[cutfade] {slot['label']} EOS BEFORE "
              f"prime_trigger (threshold={prime_at / 1e9:.2f}s "
              f"source={dur_source}); BACKUP advance",
              file=sys.stderr)
        fired[0] = True
        if (not fade_state["in_flight"]
                and current_slot_idx[0] == slot_idx
                and not advance_scheduled[slot_idx]):
            advance_scheduled[slot_idx] = True
            GLib.idle_add(prime_next)
        else:
            print(f"[cutfade] {slot['label']} EOS BACKUP "
                  "advance suppressed (fade in flight, slot "
                  "not current, or advance already scheduled)",
                  file=sys.stderr)
        return Gst.PadProbeReturn.OK

    buffer_probe_id = video_pad.add_probe(
        Gst.PadProbeType.BUFFER, _on_buffer
    )
    slot["prime_trigger_probe_id"] = buffer_probe_id
    eos_probe_id = video_pad.add_probe(
        Gst.PadProbeType.EVENT_DOWNSTREAM, _on_event
    )
    slot["prime_eos_probe_id"] = eos_probe_id
    return False


# --- Prime next clip into the OFF slot ----------------------------------

def prime_next():
    """Build the next playlist clip into the OFF slot, gate on its
    first frame, then start_fade. Returns False so callers via
    GLib.idle_add fire only once."""
    if fade_state["in_flight"]:
        # Per QA 5ec8044 soak: short clips cross their pts
        # threshold while the prior fade is still in flight. The
        # old behavior dropped the call and the promised "next
        # prime will fire on the new current" never happened ->
        # the new current ran to natural EOS, repeat-after-eos
        # froze its composite, and FROZEN WATCHDOG had to rescue
        # it 3s later (visible freeze + screen=0 churn). Fix:
        # DEFER the prime onto pending_prime; finish_fade re-fires
        # it for the new current immediately after the swap.
        print("[cutfade] prime_next called while fade in flight; "
              "DEFERRING (finish_fade will re-fire for new current)",
              file=sys.stderr)
        pending_prime[0] = True
        return False
    off_idx = 1 - current_slot_idx[0]
    if slots[off_idx]["sub"] is not None:
        print(f"[cutfade] prime_next: slot {off_idx} already "
              "occupied; skipping", file=sys.stderr)
        return False
    clip_idx = next_clip_idx[0] % len(VIDEOS)
    next_clip_idx[0] += 1
    print(f"[cutfade] prime next: clip {clip_idx} -> slot {off_idx}",
          file=sys.stderr)
    slot = build_slot(off_idx, clip_idx)
    slot["mix_sink"].set_property("alpha", 0.0)
    slot["mix_sink"].set_property("zorder", 1)  # incoming on top
    slots[current_slot_idx[0]]["mix_sink"].set_property("zorder", 0)

    # First-frame gate on the new decoder.
    dec_src = slot["dec"].get_static_pad("src")
    if dec_src is None:
        die(f"[{slot['label']}] v4l2h264dec has no src pad")

    gate = {"fired": False}

    def _on_first_frame(_pad, _info):
        if not gate["fired"]:
            gate["fired"] = True
            print(f"[cutfade] {slot['label']} first frame -> "
                  "scheduling fade", file=sys.stderr)
            GLib.idle_add(start_fade, off_idx)
            # Mark probe as auto-removed so retire does not try
            # to remove it again (gst warns "pad has no probe with
            # id N" on double-removal).
            slot["first_frame_probe_id"] = None
            return Gst.PadProbeReturn.REMOVE
        return Gst.PadProbeReturn.OK
    probe_id = dec_src.add_probe(
        Gst.PadProbeType.BUFFER, _on_first_frame
    )
    slot["first_frame_probe_id"] = probe_id

    # Deadline timer: if gate doesn't fire, start_fade anyway
    # (hold-last-frame fallback semantic of glvideomixer).
    def _deadline():
        if gate["fired"]:
            return False
        gate["fired"] = True  # gate flag to prevent later
                              # probe call from also firing
        print(f"[cutfade] {slot['label']} GATE DEADLINE "
              f"({GATE_DEADLINE_MS}ms) -- "
              "no first frame; starting fade anyway "
              "(hold-last-frame fallback)", file=sys.stderr)
        GLib.idle_add(start_fade, off_idx)
        return False
    GLib.timeout_add(GATE_DEADLINE_MS, _deadline)

    slot["sub"].sync_state_with_parent()
    if (slot["sub"].set_state(Gst.State.PLAYING)
            == Gst.StateChangeReturn.FAILURE):
        die(f"[{slot['label']}] sub set_state PLAYING failed")
    return False


# --- Fade animation -----------------------------------------------------

def start_fade(incoming_slot_idx):
    """Start the 1s alpha animation. Outgoing alpha 1->0, incoming
    alpha 0->1. Returns False so GLib.idle_add fires once."""
    if fade_state["in_flight"]:
        return False
    fade_state["incoming_slot"] = incoming_slot_idx
    fade_state["outgoing_slot"] = current_slot_idx[0]
    fade_state["start_ns"] = GLib.get_monotonic_time() * 1000
    fade_state["in_flight"] = True
    print(f"[cutfade] FADE start: slot {fade_state['outgoing_slot']}"
          f" -> slot {incoming_slot_idx}", file=sys.stderr)
    GLib.timeout_add(FADE_TICK_MS, fade_tick)
    return False


def fade_tick():
    outgoing = slots[fade_state["outgoing_slot"]]
    incoming = slots[fade_state["incoming_slot"]]
    if outgoing["mix_sink"] is None or incoming["mix_sink"] is None:
        # Slot was retired mid-fade somehow; abort fade
        print("[cutfade] fade_tick: slot mix_sink None; aborting",
              file=sys.stderr)
        fade_state["in_flight"] = False
        return False
    elapsed_s = (
        (GLib.get_monotonic_time() * 1000 - fade_state["start_ns"])
        / 1e9
    )
    if elapsed_s >= FADE_S:
        outgoing["mix_sink"].set_property("alpha", 0.0)
        incoming["mix_sink"].set_property("alpha", 1.0)
        finish_fade()
        return False
    t = elapsed_s / FADE_S
    outgoing["mix_sink"].set_property("alpha", 1.0 - t)
    incoming["mix_sink"].set_property("alpha", t)
    return True


def finish_fade():
    """Called when fade_tick reaches elapsed >= FADE_S. Swap which
    slot is current, schedule retire of the old current, and attach
    the next prime trigger to the new current."""
    outgoing_idx = fade_state["outgoing_slot"]
    incoming_idx = fade_state["incoming_slot"]
    current_slot_idx[0] = incoming_idx
    fade_state["in_flight"] = False
    # The new current is fresh: its succession has not yet been
    # scheduled. Clear the flag so the new triggers can run.
    advance_scheduled[incoming_idx] = False
    # Reset FROZEN tracking on slot swap -- the new current's
    # decoder will start producing buffers with a brand-new PTS
    # sequence and the prior "last seen" was for the outgoing.
    watchdog_state["last_dec_pts_ns"] = 0
    watchdog_state["frozen_seconds"] = 0
    print(f"[cutfade] FADE complete; current is now slot "
          f"{incoming_idx}; retiring slot {outgoing_idx}",
          file=sys.stderr)
    # Retire outgoing on next idle. QA caught my prior soak-killer:
    # GLib.idle_add(GLib.PRIORITY_LOW, callable, ...) raises
    # TypeError on this PyGObject ("Callback needs to be a function
    # or method not int") -- the canonical PyGObject signature is
    # function-first, with priority as a kwarg. Per QA's
    # simplest/safest: drop the priority entirely. retire workload
    # is small (NULL teardown of a single sub-bin) so default
    # priority is fine.
    GLib.idle_add(retire_slot, outgoing_idx)
    # Attach prime trigger to the new current. Fresh closure each
    # call -> fired[0]=False, so even non-deferred cases re-trigger
    # cleanly on the new current's pts threshold.
    attach_prime_trigger(incoming_idx)

    # Per QA 5ec8044 soak: if prime_next was DEFERRED during this
    # fade (pts threshold crossed mid-fade on a short clip),
    # fire it now for the new current. Set advance_scheduled
    # for the incoming so the just-attached pts trigger does NOT
    # race us (the trigger will idle_add prime_next; the slot-
    # occupied guard in prime_next then catches the duplicate).
    # If the new current is ALREADY past its own threshold (short
    # clip case is exactly this), the buffer probe would fire on
    # the next decoded buffer anyway; the deferred path just
    # fires sooner and skips the "wait for next buffer" latency.
    had_pending = pending_prime[0]
    pending_prime[0] = False
    if had_pending:
        print(f"[cutfade] finish_fade: re-firing DEFERRED prime "
              f"for new current slot {incoming_idx}",
              file=sys.stderr)
        advance_scheduled[incoming_idx] = True
        GLib.idle_add(prime_next)


# --- Instrument + watchdog ---------------------------------------------

screen_state = {"frames": 0, "last_ns": 0, "max_gap_ms": 0.0,
                "boundary_warns_total": 0}

try:
    log_file = open("/tmp/cutfade_fps.log", "w", buffering=1)
    log_file.write(f"# cutfade run start pid={os.getpid()}\n")
    print("[cutfade] tee fps to /tmp/cutfade_fps.log "
          "(truncated on open)", file=sys.stderr)
except OSError as _exc:
    log_file = None
    print(f"[cutfade] /tmp/cutfade_fps.log unavailable ({_exc}); "
          "stderr only", file=sys.stderr)


def attach_screen_probe():
    sink_pad = sink.get_static_pad("sink")
    if sink_pad is None:
        die("output sink has no sink pad")

    def _probe(_pad, _info):
        now = time.monotonic_ns()
        screen_state["frames"] += 1
        last = screen_state["last_ns"]
        if last:
            gap_ms = (now - last) / 1e6
            if gap_ms > screen_state["max_gap_ms"]:
                screen_state["max_gap_ms"] = gap_ms
            if gap_ms > 50:
                screen_state["boundary_warns_total"] += 1
        screen_state["last_ns"] = now
        return Gst.PadProbeReturn.OK
    sink_pad.add_probe(Gst.PadProbeType.BUFFER, _probe)


_t_start = time.monotonic()


def fps_tick():
    uptime = int(time.monotonic() - _t_start)
    cur = current_slot_idx[0]
    cur_label = slots[cur]["label"] or "-"
    off = 1 - cur
    off_label = slots[off]["label"] or "-"
    fade_str = "in_flight" if fade_state["in_flight"] else "idle"
    cur_dec_pts_ns = slots[cur].get("last_dec_pts_ns") or 0
    cur_dec_pts_ms = cur_dec_pts_ns // 1_000_000
    sched_str = "y" if advance_scheduled[cur] else "n"
    line = (
        f"[fps] t={uptime} "
        f"screen={screen_state['frames']} "
        f"max_gap_ms={int(screen_state['max_gap_ms'])} "
        f"cur={cur_label}(slot{cur}) "
        f"off={off_label}(slot{off}) "
        f"fade={fade_str} "
        f"dec_pts_ms={cur_dec_pts_ms} "
        f"sched={sched_str} "
        f"gap_warns_total={screen_state['boundary_warns_total']}"
    )
    print(line, file=sys.stderr, flush=True)
    if log_file is not None:
        try:
            log_file.write(line + "\n")
        except OSError:
            pass
    # Watchdog: 0 frames for 2 consecutive ticks -> force prime.
    # Legacy screen=0 path. Still useful for catching pipeline
    # death that bypasses both the pts trigger and the EOS
    # backup trigger.
    if screen_state["frames"] == 0:
        watchdog_state["zero_frame_seconds"] += 1
        if watchdog_state["zero_frame_seconds"] >= WATCHDOG_STALL_S:
            print("[cutfade] WATCHDOG: 0 frames for 2s -- "
                  "force prime + re-PLAY", file=sys.stderr)
            watchdog_state["zero_frame_seconds"] = 0
            advance_scheduled[cur] = True
            GLib.idle_add(prime_next)
            GLib.idle_add(
                lambda: pipeline.set_state(Gst.State.PLAYING) or False
            )
    else:
        watchdog_state["zero_frame_seconds"] = 0

    # Per QA d489fd8 ask #3: FROZEN watchdog. repeat-after-eos
    # can freeze a composite at 24fps so screen=24 no longer
    # proves motion. If the current slot's decoder PTS has not
    # advanced since the last tick AND no fade is in flight AND
    # no advance is already scheduled for this slot, count it
    # as a frozen second. After FROZEN_STALL_S consecutive
    # seconds, force-advance via prime_next. Cleared on natural
    # progress, on slot swap (finish_fade), or when the
    # zero-frame branch above force-advances.
    if (cur_dec_pts_ns > 0
            and not fade_state["in_flight"]
            and not advance_scheduled[cur]):
        if cur_dec_pts_ns == watchdog_state["last_dec_pts_ns"]:
            watchdog_state["frozen_seconds"] += 1
            if (watchdog_state["frozen_seconds"]
                    >= FROZEN_STALL_S):
                print(f"[cutfade] FROZEN WATCHDOG: dec_pts on "
                      f"slot {cur} unchanged for "
                      f"{FROZEN_STALL_S}s (pts_ms="
                      f"{cur_dec_pts_ms}) -- force advance",
                      file=sys.stderr)
                watchdog_state["frozen_seconds"] = 0
                advance_scheduled[cur] = True
                GLib.idle_add(prime_next)
        else:
            watchdog_state["frozen_seconds"] = 0
    else:
        watchdog_state["frozen_seconds"] = 0
    watchdog_state["last_dec_pts_ns"] = cur_dec_pts_ns

    screen_state["frames"] = 0
    screen_state["max_gap_ms"] = 0.0
    return True


# --- Bus + signals + run -----------------------------------------------

def on_bus(_bus, msg):
    if msg.type == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        src = msg.src.get_name() if msg.src else "?"
        print(f"[cutfade] ERROR {src}: {err.message}",
              file=sys.stderr)
        if dbg:
            print(f"[cutfade]  debug: {dbg}", file=sys.stderr)
        loop.quit()
    elif msg.type == Gst.MessageType.EOS:
        # Should not happen -- each clip's EOS is absorbed by the
        # fade-then-retire cycle. If it reaches the bus, something
        # escaped. Watchdog also catches it.
        print("[cutfade] pipeline EOS (unexpected)",
              file=sys.stderr)
        loop.quit()


bus = pipeline.get_bus()
bus.add_signal_watch()
bus.connect("message", on_bus)


def shutdown(*_):
    GLib.idle_add(loop.quit)


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)


# --- Initial setup + run ------------------------------------------------

print("==RUN-START== cutfade", file=sys.stderr)
print(f"[cutfade] PRIME_LEAD_S={PRIME_LEAD_S} FADE_S={FADE_S} "
      f"GATE_DEADLINE_MS={GATE_DEADLINE_MS} sink={SINK_CHOICE}",
      file=sys.stderr)

# Build the initial slot (slot 0, VIDEOS[0]) before going PLAYING.
build_slot(0, next_clip_idx[0])
next_clip_idx[0] += 1
slots[0]["mix_sink"].set_property("alpha", 1.0)
slots[0]["mix_sink"].set_property("zorder", 0)

# Preroll the pipeline.
if pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
    die("set_state PAUSED failed")
prerolled = False
for elapsed in range(1, PREROLL_BUDGET_S + 1):
    ret, cur, pending = pipeline.get_state(1 * Gst.SECOND)
    if ret == Gst.StateChangeReturn.SUCCESS:
        print(f"[cutfade] preroll done after ~{elapsed}s "
              f"state={cur.value_nick}", file=sys.stderr)
        prerolled = True
        break
    if ret == Gst.StateChangeReturn.NO_PREROLL:
        print(f"[cutfade] preroll NO_PREROLL after ~{elapsed}s "
              "(live source; accepting)", file=sys.stderr)
        prerolled = True
        break
    if ret == Gst.StateChangeReturn.FAILURE:
        die(f"preroll FAILURE")
    print(f"[cutfade] preroll... state={cur.value_nick} "
          f"pending={pending.value_nick} "
          f"({elapsed}/{PREROLL_BUDGET_S}s)", file=sys.stderr)
if not prerolled:
    die(f"preroll exceeded {PREROLL_BUDGET_S}s budget")

if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
    die("set_state PLAYING failed")

attach_screen_probe()
# Attach the first prime trigger on the initial slot. May reschedule
# itself if the demuxer's video src pad isn't ready yet.
attach_prime_trigger(0)
GLib.timeout_add(1000, fps_tick)

try:
    loop.run()
finally:
    print("[cutfade] shutdown -> NULL", file=sys.stderr)
    pipeline.set_state(Gst.State.NULL)
    if log_file is not None:
        try:
            log_file.close()
        except OSError:
            pass
