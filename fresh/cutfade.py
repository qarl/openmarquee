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

LOOP MECHANISM (per QA scheduler-redesign spec 2026-06-18):
  Single linear state machine driven by scheduler_tick at
  SCHEDULER_TICK_MS=50ms cadence. scheduler_tick is the SOLE
  mutator of sched["state"]:

    PLAYING_CURRENT  -- visible clip; tick re-evaluates
                        should_start_priming() each cycle:
                        (a) MIN_DWELL_S wall-clock gate
                        (cascade killer), (b) adaptive lead
                        min(PRIME_LEAD_S+FADE_S, dur*0.4), or
                        (c) FROZEN_TIMEOUT_S backstop.
       |
       v  _do_start_priming() asserts off slot empty,
          builds sub-bin, attaches passive first-frame probe.

    PRIMING_NEXT     -- incoming decoder warming. Advance on
                        first_frame_arrived OR PRIMING_DEADLINE_S.
       |
       v  _do_start_fade()

    FADING           -- _animate_fade() runs per tick; advance on
                        elapsed >= FADE_S.
       |
       v  _do_complete_fade(): SYNCHRONOUS retire (blocks NULL
          via get_state 2s) + swap + duration query.

  Probe callbacks may set passive flags only; MUST NOT call
  _do_* or write sched["state"]. The single-in-flight invariant
  + the assert in _do_start_priming convert any residual race
  into a loud assertion error instead of a silent qtdemux
  not-linked / Internal-data-stream pipeline crash.

  Per-slot pad-added handler ID + first-frame probe ID +
  dec_pts probe ID are stored on the slot dict for retire_slot
  to disconnect/remove (cutloop's proven sequence).

  retire_slot is synchronous and sends EOS to the kept
  mix.sink pad to engage repeat-after-eos before unlink + alpha=0
  + sub NULL + pipeline.remove. NEVER release_request_pad
  on a glvideomixer sink pad.

OBSERVABILITY: fps_tick at 1Hz emits a [fps] line with
state=, dec_pts_ms=, cur_dur_s=, screen=, max_gap_ms=. The
legacy screen=0 watchdog and FROZEN dec-PTS watchdog are
DELETED: under repeat-after-eos a frozen composite can drive
screen>0 falsely, so neither signal is informative anymore.
The FROZEN_TIMEOUT_S backstop inside should_start_priming
replaces both.
"""

import gc
import glob
import os
import signal
import subprocess
import sys
import threading
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

PRIME_LEAD_S = 2.0           # absolute lead cap (s). Bumped
                             # from 1.5 per QA 50e791c soak: with
                             # PRIMING_DEADLINE_S 2.0 + FADE_S
                             # 1.0 the new fractional lead needs
                             # 3.0s for fit on long clips, so the
                             # cap must not clip below that.
FADE_S = 1.0                 # crossfade duration (s)
PREROLL_BUDGET_S = 30        # GL cold-start budget under systemd

# Scheduler constants per QA spec (single linear state machine
# replacing the 4-scheduler/2-flag tangle of 5ec8044+1777846).
SCHEDULER_TICK_MS   = 50     # state-machine tick cadence (ms)
MIN_DWELL_S         = 0.5    # cascade killer: clip must be
                             # visible >= MIN_DWELL_S wall-clock
                             # as current before any advance can
                             # start. Measured from
                             # sched["became_current_ns"], NOT
                             # from clip PTS (the incoming was
                             # primed early so its PTS at swap is
                             # already ~FADE_S into the file).
PRIME_LEAD_FRACTION = 0.6    # adaptive lead = min(
                             # PRIME_LEAD_S + FADE_S,
                             # dur_s * PRIME_LEAD_FRACTION).
                             # Bumped 0.4 -> 0.6 per QA 50e791c
                             # soak: with the 2.0s priming
                             # deadline a short 4.75s clip needs
                             # dur*0.6 = 2.85s of runway so
                             # prime + first-frame + fade fits
                             # before/around outgoing EOS (any
                             # overshoot is covered by retire
                             # EOS + repeat-after-eos freeze).
PRIMING_DEADLINE_S  = 2.0    # max wait for incoming first
                             # frame. Bumped 0.5 -> 2.0 per QA
                             # 50e791c soak: bcm2835 first frame
                             # takes up to ~2s (matches the
                             # proven d489fd8 GATE_DEADLINE_MS
                             # budget). 0.5s caused the deadline
                             # to fire EVERY cycle -> every fade
                             # started on a not-yet-decoded
                             # incoming = black fade-in. The
                             # deadline log stays in
                             # _do_start_fade so a future cold-
                             # start regression surfaces.
FROZEN_TIMEOUT_S    = 12.0   # backstop: max wall-clock in
                             # PLAYING_CURRENT before force-
                             # advance. Replaces the deleted
                             # FROZEN-watchdog (3s dec PTS) and
                             # screen=0-watchdog. No separate
                             # timer; lives as a single branch
                             # inside should_start_priming().

# Fallback duration if qtdemux query_duration fails. Per QA
# assets are uniform h264 Main 1280x720 24fps but vary in length;
# if a specific asset's query fails the fallback is ~6s.
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
     "dec_pts_probe_id": None, "last_dec_pts_ns": 0},
    {"sub": None, "dec": None, "demux": None, "mix_sink": None,
     "label": None, "serial": None, "dur_ns": 0,
     "pad_added_id": None, "first_frame_probe_id": None,
     "dec_pts_probe_id": None, "last_dec_pts_ns": 0},
]
current_slot_idx = [0]   # which slots[] entry is the active one
next_clip_idx = [0]       # cycling counter through VIDEOS

# Cycle serial (every spawned bin gets a unique id for log clarity)
spawn_serial = [0]

# Per QA 50e791c soak: retire #23 produced an intermittent
# qtdemux not-linked / Internal-data-stream crash despite sync
# retire + the assert. The dying outgoing qtdemux's streaming
# thread pushes a buffer just after we unlink the mix peer ->
# qtdemux posts a fatal-looking error on the bus.
#
# Defensive part (B) per QA: track currently-retiring sub-bin
# names; on_bus's ERROR handler ignores errors whose source
# element lives inside any of those bins (the state machine has
# already moved on; a teardown-window error is benign).
#
# Set is touched only on the GLib main loop thread (retire_slot
# add/discard, on_bus read) so no lock needed.
retiring_bin_names = set()


def _is_in_retiring_bin(elem):
    """Walk elem's parent chain; True if any ancestor's name is
    in retiring_bin_names. Used by on_bus to treat teardown-race
    errors as benign."""
    while elem is not None:
        try:
            name = elem.get_name()
        except Exception:
            return False
        if name in retiring_bin_names:
            return True
        try:
            elem = elem.get_parent()
        except Exception:
            return False
    return False


# Single linear-state-machine state per QA scheduler spec. ONLY
# mutated inside scheduler_tick's three transition helpers
# (_do_start_priming, _do_start_fade, _do_complete_fade). Read by
# scheduler_tick's predicates. Probe callbacks set passive flags
# (next_first_frame_arrived) but MUST NOT mutate state.
#
# Fields:
#   state                    : "PLAYING_CURRENT" | "PRIMING_NEXT" | "FADING"
#   became_current_ns        : monotonic ns; written at startup and
#                              inside _do_complete_fade. Used by
#                              MIN_DWELL + FROZEN_TIMEOUT gates.
#   current_clip_dur_s       : duration of the clip currently
#                              displayed (seconds). Written at
#                              startup and in _do_complete_fade.
#   priming_started_ns       : monotonic ns; written in
#                              _do_start_priming. Used by
#                              _priming_deadline.
#   next_first_frame_arrived : bool. Set True by the incoming
#                              decoder's first-frame probe.
#                              Cleared in _do_start_priming.
#   fade_started_ns          : monotonic ns; written in
#                              _do_start_fade. Used by
#                              _fade_animation_done + _animate_fade.
#   outgoing_slot_idx        : int; written in _do_start_fade.
#   incoming_slot_idx        : int; written in _do_start_fade.
sched = {
    "state": "PLAYING_CURRENT",
    "became_current_ns": 0,
    "current_clip_dur_s": 0.0,
    "priming_started_ns": 0,
    "next_first_frame_arrived": False,
    "fade_started_ns": 0,
    "outgoing_slot_idx": -1,
    "incoming_slot_idx": -1,
}


def _slot_clear(slot):
    """Reset slot dict to "empty" state (does not touch gst -- caller
    must have already retired the elements)."""
    for k in ("sub", "dec", "demux", "mix_sink", "label", "serial",
              "pad_added_id", "first_frame_probe_id",
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


def _await_idle(pad, timeout_s, label=""):
    """One-shot IDLE-pad wait with short timeout. Returns True if
    pad was observed idle, False on timeout.

    Per QA 50e791c coordination note (after a 1/23 not-linked
    teardown race on synchronous retire): use an IDLE probe
    (REMOVE on fire) with a SHORT timeout and PROCEED on timeout,
    NOT a permanent BLOCK probe. A raw BLOCK probe can deadlock
    the NULL transition because it holds the streaming thread.
    IDLE fires when the pad currently has no buffer in flight
    (or immediately on the calling thread if the pad is already
    idle). The defensive (B) bus handler ignore-retiring-bin
    check catches any teardown-race error if a buffer slips
    through after the IDLE wait returns."""
    evt = threading.Event()

    def _on_idle(_pad, _info):
        evt.set()
        return Gst.PadProbeReturn.REMOVE

    probe_id = pad.add_probe(Gst.PadProbeType.IDLE, _on_idle)
    if probe_id == 0:
        print(f"[cutfade] _await_idle {label} add_probe "
              "returned 0", file=sys.stderr)
        return False
    if not evt.wait(timeout=timeout_s):
        print(f"[cutfade] _await_idle {label} did NOT idle "
              f"within {timeout_s:.2f}s; proceeding (bus "
              "handler will ignore any teardown-race error)",
              file=sys.stderr)
        try:
            pad.remove_probe(probe_id)
        except Exception:
            pass
        return False
    return True


def retire_slot(slot_idx):
    """Tear down the sub-bin in slots[slot_idx]. SYNCHRONOUS per
    QA scheduler spec section (4): blocks until the sub is fully
    NULL'd and slot fields are cleared. Called only from
    _do_complete_fade. Uses cutloop's proven disconnect-before-
    NULL sequence to prevent closure-ref leaks (Python closures
    captured by the pad-added handler + any probes hold the
    sub-bin alive otherwise).

    Per QA review note #1: the blocking NULL via get_state(2s)
    will produce a ~1s main-loop pause at each boundary (=
    the known secondary boundary-hitch, now systematic).
    EXPECTED and ACCEPTED this round; smoothing is the final
    step after the reel cycles crash-free.

    Per QA 50e791c soak teardown-race fix:
    - Part (A): _await_idle(glupload.src, 0.1) BEFORE unlink so
      no buffer is in flight on the chain at the unlink instant.
      Proceed-on-timeout so retire never deadlocks the NULL.
    - Part (B): retiring_bin_names tracks this sub-bin's name
      across the whole teardown so on_bus's ERROR handler can
      treat a streaming-thread error from inside as benign and
      log + ignore (not loop.quit). Tracked via try/finally so
      even an exception in teardown still discards the name."""
    slot = slots[slot_idx]
    sub = slot["sub"]
    if sub is None:
        return
    label = slot["label"] or f"slot_{slot_idx}"
    sub_name = sub.get_name()
    weak = weakref.ref(sub)

    # Part (B): announce this bin as retiring BEFORE we touch
    # anything else, so on_bus catches any error from the
    # streaming thread during the entire teardown window.
    retiring_bin_names.add(sub_name)
    try:
        try:
            # 1. Disconnect pad-added handler (closure-leak prevention).
            if slot["pad_added_id"] and slot["demux"] is not None:
                try:
                    slot["demux"].disconnect(slot["pad_added_id"])
                except Exception as exc:
                    print(f"[cutfade] retire {label} pad-added "
                          f"disconnect WARN: {exc}",
                          file=sys.stderr)
            # 2. Remove decoder.src probes (first-frame + dec_pts).
            if slot["dec"] is not None:
                dec_src = slot["dec"].get_static_pad("src")
                if dec_src is not None:
                    if slot["first_frame_probe_id"]:
                        try:
                            dec_src.remove_probe(
                                slot["first_frame_probe_id"]
                            )
                        except Exception as exc:
                            print(f"[cutfade] retire {label} "
                                  f"first-frame probe WARN: "
                                  f"{exc}", file=sys.stderr)
                    if slot["dec_pts_probe_id"]:
                        try:
                            dec_src.remove_probe(
                                slot["dec_pts_probe_id"]
                            )
                        except Exception as exc:
                            print(f"[cutfade] retire {label} "
                                  f"dec_pts probe WARN: {exc}",
                                  file=sys.stderr)
            # 3. EOS into the kept mix sink pad to ENGAGE
            # repeat-after-eos before unlinking (proven on
            # d489fd8). send_event is synchronous on the
            # aggregator sink-event handler so priv->eos = True
            # by return.
            if slot["mix_sink"] is not None:
                if not slot["mix_sink"].send_event(
                    Gst.Event.new_eos()
                ):
                    print(f"[cutfade] retire {label} mix_sink "
                          "EOS send_event returned False "
                          "(proceeding)", file=sys.stderr)
                # 4. Part (A): IDLE-pad wait on the outgoing
                # branch's peer (= glupload.src, reached via the
                # ghost's target on mix_sink's peer). Short
                # timeout; proceed on timeout. Prevents the
                # qtdemux not-linked race observed on 50e791c
                # retire #23.
                peer = slot["mix_sink"].get_peer()
                if peer is not None:
                    # peer is the ghost pad on the sub-bin;
                    # its target IS glupload.src.
                    target = peer.get_target() if hasattr(
                        peer, "get_target"
                    ) else None
                    idle_target = target if target is not None else peer
                    _await_idle(idle_target, 0.1,
                                label=f"{label} glupload.src")
                    # 5. Unlink old peer from kept mix sink pad.
                    peer.unlink(slot["mix_sink"])
                # alpha=0 -> composite path skips the frozen frame.
                slot["mix_sink"].set_property("alpha", 0.0)
            # 6. BLOCKING set_state(NULL). ASYNC -> wait up to 2s.
            ret = sub.set_state(Gst.State.NULL)
            if ret == Gst.StateChangeReturn.ASYNC:
                ret2, _cur, _pend = sub.get_state(2 * Gst.SECOND)
                if ret2 != Gst.StateChangeReturn.SUCCESS:
                    print(f"[cutfade] retire {label} NULL "
                          f"get_state WARN ret="
                          f"{ret2.value_nick} after 2s "
                          "(proceeding; downstream assert will "
                          "surface corrupted state)",
                          file=sys.stderr)
            # 7. pipeline.remove.
            pipeline.remove(sub)
            print(f"[cutfade] retire {label} (slot {slot_idx}; "
                  "mix.sink pad kept allocated)",
                  file=sys.stderr)
        except Exception as exc:
            print(f"[cutfade] retire {label} WARN: {exc}",
                  file=sys.stderr)

        # 8. Clear slot fields (mix_sink NOT cleared -- persistent).
        _slot_clear(slot)
    finally:
        # Part (B): even if teardown raised, drop this bin from
        # the retiring set so on_bus stops ignoring its errors
        # (any subsequent error from this name IS real).
        retiring_bin_names.discard(sub_name)

    # 8. Defensive leak check (existing cf03a8f instrumentation).
    def _check_leak():
        gc.collect()
        if weak() is not None:
            print(f"[cutfade] [retire] LEAK ref still held for "
                  f"{label}", file=sys.stderr)
        return False
    GLib.idle_add(_check_leak)


# --- Scheduler state machine -------------------------------------------
#
# Per QA scheduler-redesign spec (replaces the 4-scheduler /
# 2-flag tangle of 5ec8044+1777846 that oscillated between
# drop-prime-freeze and defer-prime-cascade failure modes).
#
# Single linear state machine:
#   PLAYING_CURRENT -> PRIMING_NEXT -> FADING -> PLAYING_CURRENT
#
# Driven by scheduler_tick at SCHEDULER_TICK_MS cadence.
# scheduler_tick is the SOLE mutator of sched["state"]. Probe
# callbacks may set passive flags (next_first_frame_arrived) but
# MUST NOT call _do_* or write sched["state"].
#
# Gates that kill the prior failure modes:
#   - MIN_DWELL_S wall-clock from became_current_ns: a newly-
#     current short clip cannot immediately re-trigger ->
#     cascade dead at source.
#   - Adaptive lead min(PRIME_LEAD_S + FADE_S, dur * 0.4):
#     short clips get a smaller lead -> no overshoot off end.
#   - FROZEN_TIMEOUT_S backstop branch INSIDE
#     should_start_priming: no separate timer, no race with
#     the primary trigger predicate.
#
# Single-in-flight invariant enforcement:
#   1. sched["state"] = ... appears only in the three _do_*
#      transition helpers. Lexical search confirms.
#   2. scheduler_tick runs on the GLib main loop (single thread).
#   3. The first-frame probe and any other event callbacks may
#      only set passive flags.
#   4. retire_slot is synchronous (blocks NULL via get_state)
#      so FADING -> PLAYING_CURRENT is atomic from the state
#      machine's perspective.
#   5. _do_start_priming asserts slots[off_idx]["sub"] is None
#      before building -- turns any residual race into a loud
#      assertion error instead of the silent qtdemux not-linked
#      / Internal-data-stream pipeline crash.


def _query_clip_dur_s(slot_idx):
    """Best-effort duration in seconds for slot's clip.
    Try demux.query_duration, then per-pad query_duration on the
    video src pad, then DUR_FALLBACK_NS with a WARN log."""
    slot = slots[slot_idx]
    demux = slot.get("demux")
    if demux is None:
        print(f"[cutfade] WARN _query_clip_dur_s slot {slot_idx} "
              "demux is None; using fallback",
              file=sys.stderr)
        return DUR_FALLBACK_NS / 1e9
    ok, dur = demux.query_duration(Gst.Format.TIME)
    if ok and dur > 0:
        return dur / 1e9
    try:
        it = demux.iterate_src_pads()
        while True:
            res, p = it.next()
            if res != Gst.IteratorResult.OK:
                break
            if p.get_name().startswith("video"):
                ok, dur = p.query_duration(Gst.Format.TIME)
                if ok and dur > 0:
                    return dur / 1e9
                break
    except Exception as exc:
        print(f"[cutfade] WARN _query_clip_dur_s slot {slot_idx} "
              f"pad query exc: {exc}", file=sys.stderr)
    print(f"[cutfade] WARN _query_clip_dur_s slot {slot_idx} "
          f"both queries failed; fallback "
          f"{DUR_FALLBACK_NS / 1e9:.2f}s", file=sys.stderr)
    return DUR_FALLBACK_NS / 1e9


def _query_current_pts_ns():
    """Return the current decoder's playback position in ns, or
    -1 if the query fails. Used by should_start_priming's
    adaptive-lead remaining-time math."""
    cur = current_slot_idx[0]
    dec = slots[cur].get("dec")
    if dec is None:
        return -1
    try:
        ok, pos = dec.query_position(Gst.Format.TIME)
        if ok and pos >= 0:
            return pos
    except Exception:
        pass
    return -1


# --- Predicates --------------------------------------------------------

def should_start_priming():
    """Return True iff PLAYING_CURRENT should advance to
    PRIMING_NEXT. Two gates + one backstop per spec section (2).

    Wall-clock dwell + remaining-time test, NOT a PTS threshold:
    the incoming clip was primed during the prior fade so by the
    time it becomes current its PTS is already ~FADE_S into the
    file. A PTS-based threshold races against the swap; the
    wall-clock dwell is independent of clip-internal time."""
    now_ns = GLib.get_monotonic_time() * 1000
    time_as_current_s = (
        (now_ns - sched["became_current_ns"]) / 1e9
    )

    # (a) MIN_DWELL -- cascade killer. First gate.
    if time_as_current_s < MIN_DWELL_S:
        return False

    # (c) FROZEN BACKSTOP -- replaces FROZEN-watchdog and
    # screen=0-watchdog. Loud-log when this fires.
    if time_as_current_s > FROZEN_TIMEOUT_S:
        print(f"[cutfade] FROZEN BACKSTOP: forcing advance from "
              f"PLAYING_CURRENT (time_as_current="
              f"{time_as_current_s:.2f}s, dur="
              f"{sched['current_clip_dur_s']:.2f}s)",
              file=sys.stderr)
        return True

    # (b) ADAPTIVE LEAD -- replaces pts-threshold + EOS-backup.
    dur_s = sched["current_clip_dur_s"]
    if dur_s <= 0:
        return False  # duration unknown yet; wait for next tick.
    adaptive_lead_s = min(PRIME_LEAD_S + FADE_S,
                          dur_s * PRIME_LEAD_FRACTION)

    current_pts_ns = _query_current_pts_ns()
    if current_pts_ns < 0:
        return False  # position query failed; FROZEN catches.
    remaining_s = dur_s - (current_pts_ns / 1e9)
    return remaining_s <= adaptive_lead_s


def _priming_done():
    """True once the incoming decoder's first frame has been
    observed (passive flag set by the first-frame probe)."""
    return sched["next_first_frame_arrived"]


def _priming_deadline():
    """True once PRIMING_NEXT has been in flight beyond
    PRIMING_DEADLINE_S wall-clock."""
    now_ns = GLib.get_monotonic_time() * 1000
    elapsed_s = (now_ns - sched["priming_started_ns"]) / 1e9
    return elapsed_s > PRIMING_DEADLINE_S


def _fade_animation_done():
    """True once FADING has been in flight beyond FADE_S
    wall-clock."""
    now_ns = GLib.get_monotonic_time() * 1000
    elapsed_s = (now_ns - sched["fade_started_ns"]) / 1e9
    return elapsed_s >= FADE_S


def _animate_fade():
    """Per-tick alpha update during FADING. Outgoing 1->0,
    incoming 0->1, linear over FADE_S."""
    outgoing_idx = sched["outgoing_slot_idx"]
    incoming_idx = sched["incoming_slot_idx"]
    out_pad = slots[outgoing_idx].get("mix_sink")
    in_pad = slots[incoming_idx].get("mix_sink")
    if out_pad is None or in_pad is None:
        return
    now_ns = GLib.get_monotonic_time() * 1000
    elapsed_s = (now_ns - sched["fade_started_ns"]) / 1e9
    t = max(0.0, min(1.0, elapsed_s / FADE_S))
    out_pad.set_property("alpha", 1.0 - t)
    in_pad.set_property("alpha", t)


# --- Transitions -------------------------------------------------------
#
# The three _do_* helpers are the ONLY places that mutate
# sched["state"]. Each is called exclusively from scheduler_tick
# after the corresponding predicate returns True.

def _do_start_priming():
    """PLAYING_CURRENT -> PRIMING_NEXT. Builds the next sub-bin
    into the off slot, attaches a first-frame probe (passive
    flag setter only), transitions the new sub to PLAYING."""
    cur_idx = current_slot_idx[0]
    off_idx = 1 - cur_idx
    # Spec section (5) point 5: assert slot empty BEFORE building
    # to turn any single-in-flight invariant violation into a
    # loud assertion error rather than a silent downstream
    # qtdemux not-linked / Internal-data-stream pipeline crash.
    assert slots[off_idx]["sub"] is None, (
        f"_do_start_priming: slots[{off_idx}]['sub'] is not "
        "None at PLAYING_CURRENT -> PRIMING_NEXT transition; "
        "single-in-flight invariant violated"
    )

    now_ns = GLib.get_monotonic_time() * 1000
    time_as_current_s = (
        (now_ns - sched["became_current_ns"]) / 1e9
    )
    # Per QA 50e791c soak: log the pacing math at the decision
    # point so the soak shows current playback position,
    # remaining playback, and the adaptive lead the predicate
    # used to fire. Lets QA judge whether the predicate is
    # firing at the right time relative to clip-internal pts.
    dur_s = sched["current_clip_dur_s"]
    current_pts_ns = _query_current_pts_ns()
    if current_pts_ns >= 0:
        current_pts_ms = current_pts_ns // 1_000_000
        remaining_s = dur_s - (current_pts_ns / 1e9)
    else:
        current_pts_ms = -1
        remaining_s = -1.0
    if dur_s > 0:
        adaptive_lead_s = min(PRIME_LEAD_S + FADE_S,
                              dur_s * PRIME_LEAD_FRACTION)
    else:
        adaptive_lead_s = -1.0
    clip_idx = next_clip_idx[0] % len(VIDEOS)
    next_clip_idx[0] += 1
    print(f"[cutfade] [sched] PLAYING_CURRENT -> PRIMING_NEXT "
          f"(current_dur={dur_s:.2f}s, "
          f"time_as_current={time_as_current_s:.2f}s, "
          f"cur_pts_ms={current_pts_ms}, "
          f"remaining_s={remaining_s:.2f}, "
          f"adaptive_lead_s={adaptive_lead_s:.2f})",
          file=sys.stderr)
    print(f"[cutfade] prime next: clip {clip_idx} -> slot {off_idx}",
          file=sys.stderr)

    slot = build_slot(off_idx, clip_idx)
    slot["mix_sink"].set_property("alpha", 0.0)
    slot["mix_sink"].set_property("zorder", 1)   # incoming on top
    slots[cur_idx]["mix_sink"].set_property("zorder", 0)

    # Reset first-frame flag BEFORE installing the probe so a
    # late-arriving probe from a prior cycle (impossible under
    # the invariant but defensive) does not falsely satisfy
    # _priming_done.
    sched["next_first_frame_arrived"] = False

    dec_src = slot["dec"].get_static_pad("src")
    if dec_src is None:
        die(f"[{slot['label']}] v4l2h264dec has no src pad")

    # Spec section (5) point 3: the first-frame probe is a
    # PASSIVE flag setter only. MUST NOT call any _do_* function
    # and MUST NOT write sched["state"].
    def _on_first_frame(_pad, _info):
        if not sched["next_first_frame_arrived"]:
            sched["next_first_frame_arrived"] = True
            print(f"[cutfade] {slot['label']} first frame "
                  "observed (PRIMING_NEXT)", file=sys.stderr)
            slot["first_frame_probe_id"] = None
            return Gst.PadProbeReturn.REMOVE
        return Gst.PadProbeReturn.OK

    slot["first_frame_probe_id"] = dec_src.add_probe(
        Gst.PadProbeType.BUFFER, _on_first_frame
    )

    slot["sub"].sync_state_with_parent()
    if (slot["sub"].set_state(Gst.State.PLAYING)
            == Gst.StateChangeReturn.FAILURE):
        die(f"[{slot['label']}] sub set_state PLAYING failed")

    sched["priming_started_ns"] = now_ns
    sched["state"] = "PRIMING_NEXT"


def _do_start_fade():
    """PRIMING_NEXT -> FADING. Stamps fade_started_ns and the
    outgoing/incoming slot indices; alpha animation runs on
    subsequent ticks via _animate_fade.

    Per QA review note #2: if the PRIMING_DEADLINE_S deadline
    fires before the first frame arrives, log it so the soak can
    show whether a brief black-flash occurs at the start of the
    crossfade -- bump PRIMING_DEADLINE_S if it does."""
    now_ns = GLib.get_monotonic_time() * 1000
    elapsed_s = (now_ns - sched["priming_started_ns"]) / 1e9
    first_frame = sched["next_first_frame_arrived"]
    incoming_idx = 1 - current_slot_idx[0]
    # Per QA 50e791c+ ask: log the incoming decoder's latest
    # decoded PTS at the moment we start fading so the soak
    # shows where (in clip-internal time) the incoming begins
    # being visible. If the incoming runs ahead during the 2s
    # prime, it may fade in from its MIDDLE not its start --
    # the soak should make that visible without us guessing.
    incoming_first_pts_ns = (
        slots[incoming_idx].get("last_dec_pts_ns") or 0
    )
    incoming_first_pts_ms = incoming_first_pts_ns // 1_000_000
    if not first_frame:
        print(f"[cutfade] [sched] PRIMING deadline hit, "
              f"first_frame=False (elapsed={elapsed_s:.2f}s; "
              "incoming may show black at fade start -- bump "
              "PRIMING_DEADLINE_S if soak shows black-flashes)",
              file=sys.stderr)
    print(f"[cutfade] [sched] PRIMING_NEXT    -> FADING       "
          f"(first_frame={first_frame}, "
          f"elapsed={elapsed_s:.2f}s, "
          f"incoming_first_pts_ms={incoming_first_pts_ms})",
          file=sys.stderr)
    sched["outgoing_slot_idx"] = current_slot_idx[0]
    sched["incoming_slot_idx"] = incoming_idx
    sched["fade_started_ns"] = now_ns
    sched["state"] = "FADING"


def _do_complete_fade():
    """FADING -> PLAYING_CURRENT. Lock final alpha values,
    synchronously retire the outgoing sub-bin (blocks until
    NULL), swap current_slot_idx, stamp became_current_ns, and
    query the new current's duration.

    Per QA review note #1: synchronous retire WILL produce a
    ~1s main-loop pause at each boundary (= the known
    secondary boundary-hitch, now systematic). EXPECTED and
    ACCEPTED this round."""
    outgoing_idx = sched["outgoing_slot_idx"]
    incoming_idx = sched["incoming_slot_idx"]
    out_pad = slots[outgoing_idx].get("mix_sink")
    in_pad = slots[incoming_idx].get("mix_sink")
    if out_pad is not None:
        out_pad.set_property("alpha", 0.0)
    if in_pad is not None:
        in_pad.set_property("alpha", 1.0)

    retire_slot(outgoing_idx)
    # Spec section (5) point 5: assert post-retire state to turn
    # any "retire didn't fully complete" residual into a loud
    # fail rather than a downstream crash on the next prime.
    assert slots[outgoing_idx]["sub"] is None, (
        f"_do_complete_fade: slots[{outgoing_idx}]['sub'] still "
        "not None after synchronous retire_slot; the BLOCKING "
        "NULL transition did not complete"
    )

    current_slot_idx[0] = incoming_idx
    sched["became_current_ns"] = GLib.get_monotonic_time() * 1000
    sched["current_clip_dur_s"] = _query_clip_dur_s(incoming_idx)
    sched["state"] = "PLAYING_CURRENT"
    print(f"[cutfade] [sched] FADING          -> PLAYING_CURRENT "
          f"(incoming_slot={incoming_idx}, "
          f"dur={sched['current_clip_dur_s']:.2f}s)",
          file=sys.stderr)


# --- Scheduler tick ----------------------------------------------------

def scheduler_tick():
    """Sole state mutator. Called via GLib.timeout_add at
    SCHEDULER_TICK_MS cadence. Reads sched["state"], dispatches
    to at most one transition helper per tick. Returns True to
    keep the timeout firing."""
    state = sched["state"]
    if state == "PLAYING_CURRENT":
        if should_start_priming():
            _do_start_priming()
    elif state == "PRIMING_NEXT":
        if _priming_done() or _priming_deadline():
            _do_start_fade()
    elif state == "FADING":
        _animate_fade()
        if _fade_animation_done():
            _do_complete_fade()
    else:
        # Per spec section (5) point 1: sched["state"] is
        # written EXCLUSIVELY by the three _do_* helpers (and
        # the startup populate). Recovery here would violate
        # that lexical invariant. If we reach this branch the
        # implementation has been corrupted -- log every tick
        # so the soak surfaces it via the absence of [sched]
        # transitions; do not silently self-heal.
        print(f"[cutfade] scheduler_tick: unknown state "
              f"{state!r} -- no-op (invariant violated)",
              file=sys.stderr)
    return True


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
    """1Hz observability tick. Diagnostic only -- the scheduler
    state machine drives all advance decisions. The legacy
    screen=0 watchdog and FROZEN-dec-pts watchdog are DELETED
    per QA spec section (4): under repeat-after-eos a frozen
    composite can drive screen>0 falsely, so neither signal is
    informative anymore. State machine progress + the
    FROZEN_TIMEOUT_S backstop inside should_start_priming
    replace both."""
    uptime = int(time.monotonic() - _t_start)
    cur = current_slot_idx[0]
    cur_label = slots[cur]["label"] or "-"
    off = 1 - cur
    off_label = slots[off]["label"] or "-"
    cur_dec_pts_ns = slots[cur].get("last_dec_pts_ns") or 0
    cur_dec_pts_ms = cur_dec_pts_ns // 1_000_000
    line = (
        f"[fps] t={uptime} "
        f"screen={screen_state['frames']} "
        f"max_gap_ms={int(screen_state['max_gap_ms'])} "
        f"cur={cur_label}(slot{cur}) "
        f"off={off_label}(slot{off}) "
        f"state={sched['state']} "
        f"dec_pts_ms={cur_dec_pts_ms} "
        f"cur_dur_s={sched['current_clip_dur_s']:.2f} "
        f"gap_warns_total={screen_state['boundary_warns_total']}"
    )
    print(line, file=sys.stderr, flush=True)
    if log_file is not None:
        try:
            log_file.write(line + "\n")
        except OSError:
            pass
    screen_state["frames"] = 0
    screen_state["max_gap_ms"] = 0.0
    return True


# --- Bus + signals + run -----------------------------------------------

def on_bus(_bus, msg):
    if msg.type == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        src_elem = msg.src
        src_name = (
            src_elem.get_name() if src_elem is not None else "?"
        )
        # Per QA 50e791c soak teardown-race fix part (B):
        # if the error originates from inside a currently-
        # retiring sub-bin, this is the streaming thread
        # emitting after our unlink window. The state machine
        # has already moved on; the error is benign. LOG +
        # IGNORE (do NOT loop.quit). _is_in_retiring_bin walks
        # the parent chain so a deeply nested element (e.g.
        # qtdemux inside the sub-bin) is still recognized.
        if (src_elem is not None
                and _is_in_retiring_bin(src_elem)):
            print(f"[cutfade] BUS ERROR ignored "
                  f"(source {src_name} inside retiring bin): "
                  f"{err.message}", file=sys.stderr)
            if dbg:
                print(f"[cutfade]  debug: {dbg}",
                      file=sys.stderr)
            return
        print(f"[cutfade] ERROR {src_name}: {err.message}",
              file=sys.stderr)
        if dbg:
            print(f"[cutfade]  debug: {dbg}", file=sys.stderr)
        loop.quit()
    elif msg.type == Gst.MessageType.EOS:
        # Should not happen -- each clip's EOS is absorbed by
        # repeat-after-eos on the kept mix pad + the retire
        # EOS-into-mix_sink. If pipeline EOS reaches the bus,
        # something escaped the design and the loop should quit.
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
      f"MIN_DWELL_S={MIN_DWELL_S} "
      f"PRIME_LEAD_FRACTION={PRIME_LEAD_FRACTION} "
      f"PRIMING_DEADLINE_S={PRIMING_DEADLINE_S} "
      f"FROZEN_TIMEOUT_S={FROZEN_TIMEOUT_S} "
      f"SCHEDULER_TICK_MS={SCHEDULER_TICK_MS} "
      f"sink={SINK_CHOICE}",
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

# Per QA scheduler spec section "Implementation notes for code2":
# stamp became_current_ns RIGHT after PLAYING + populate sched
# state BEFORE arming scheduler_tick. current_slot_idx[0] is
# already 0 (from the slot 0 build above) so _query_current_pts_ns
# has a valid decoder once the scheduler starts ticking.
sched["state"] = "PLAYING_CURRENT"
sched["became_current_ns"] = GLib.get_monotonic_time() * 1000
sched["current_clip_dur_s"] = _query_clip_dur_s(0)
print(f"[cutfade] [sched] initial state PLAYING_CURRENT slot=0 "
      f"dur={sched['current_clip_dur_s']:.2f}s",
      file=sys.stderr)

GLib.timeout_add(SCHEDULER_TICK_MS, scheduler_tick)
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
