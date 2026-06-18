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


# --- Slot state machine -------------------------------------------------

# Two slots, each can hold a sub-bin feeding mix.sink_0 / sink_1.
# At any moment one is the CURRENT (alpha=1) and the other is
# either empty (no sub-bin) or being PRIMED (alpha=0, pre-fade).
slots = [
    {"sub": None, "dec": None, "demux": None, "mix_sink": None,
     "label": None, "serial": None, "dur_ns": 0,
     "pad_added_id": None, "first_frame_probe_id": None,
     "prime_trigger_probe_id": None},
    {"sub": None, "dec": None, "demux": None, "mix_sink": None,
     "label": None, "serial": None, "dur_ns": 0,
     "pad_added_id": None, "first_frame_probe_id": None,
     "prime_trigger_probe_id": None},
]
current_slot_idx = [0]   # which slots[] entry is the active one
next_clip_idx = [0]       # cycling counter through VIDEOS

# Fade state
fade_state = {"start_ns": 0, "in_flight": False,
              "incoming_slot": -1, "outgoing_slot": -1}

# Watchdog state
watchdog_state = {"zero_frame_seconds": 0}

# Cycle serial (every spawned bin gets a unique id for log clarity)
spawn_serial = [0]


def _slot_clear(slot):
    """Reset slot dict to "empty" state (does not touch gst -- caller
    must have already retired the elements)."""
    for k in ("sub", "dec", "demux", "mix_sink", "label", "serial",
              "pad_added_id", "first_frame_probe_id",
              "prime_trigger_probe_id"):
        slot[k] = None
    slot["dur_ns"] = 0


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
    mix_sink = mix.request_pad_simple("sink_%u")
    if mix_sink is None:
        die(f"[{label}] mix.request_pad_simple failed")
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
        if (slot["first_frame_probe_id"]
                and slot["dec"] is not None):
            dec_src = slot["dec"].get_static_pad("src")
            if dec_src is not None:
                try:
                    dec_src.remove_probe(slot["first_frame_probe_id"])
                except Exception as exc:
                    print(f"[cutfade] retire {label} first-frame "
                          f"probe WARN: {exc}", file=sys.stderr)
        if (slot["prime_trigger_probe_id"]
                and slot["demux"] is not None):
            # demux video src pad -- find it
            it = slot["demux"].iterate_src_pads()
            while True:
                res, p = it.next()
                if res != Gst.IteratorResult.OK:
                    break
                if p.get_name().startswith("video"):
                    try:
                        p.remove_probe(slot["prime_trigger_probe_id"])
                    except Exception as exc:
                        print(f"[cutfade] retire {label} prime "
                              f"trigger probe WARN: {exc}",
                              file=sys.stderr)
                    break
        sub.set_state(Gst.State.NULL)
        pipeline.remove(sub)
        if slot["mix_sink"] is not None:
            mix.release_request_pad(slot["mix_sink"])
        print(f"[cutfade] retire {label} (slot {slot_idx})",
              file=sys.stderr)
    except Exception as exc:
        print(f"[cutfade] retire {label} WARN: {exc}",
              file=sys.stderr)

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

    # Query duration on the demuxer. Per cutloop pattern, this works
    # once the moov box is parsed (which is what triggers pad-added).
    ok, dur = slot["demux"].query_duration(Gst.Format.TIME)
    if ok and dur > 0:
        slot["dur_ns"] = dur
    else:
        slot["dur_ns"] = DUR_FALLBACK_NS
        print(f"[cutfade] {slot['label']} duration query failed; "
              f"fallback {DUR_FALLBACK_NS / 1e9:.2f}s",
              file=sys.stderr)
    prime_at = slot["dur_ns"] - int(
        (PRIME_LEAD_S + FADE_S) * 1_000_000_000
    )
    if prime_at <= 0:
        # Clip too short for prime-lead arithmetic; skip prime,
        # rely on watchdog to keep things moving. Should not
        # happen for our assets but defensive.
        print(f"[cutfade] {slot['label']} dur too short for "
              f"prime arithmetic ({slot['dur_ns'] / 1e9:.2f}s); "
              "watchdog will handle", file=sys.stderr)
        return False
    print(f"[cutfade] {slot['label']} duration "
          f"{slot['dur_ns'] / 1e9:.2f}s, "
          f"prime trigger at {prime_at / 1e9:.2f}s",
          file=sys.stderr)

    fired = [False]

    def _on_buffer(_pad, info):
        if fired[0]:
            return Gst.PadProbeReturn.REMOVE
        buf = info.get_buffer()
        if buf is None or buf.pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK
        if buf.pts >= prime_at:
            fired[0] = True
            print(f"[cutfade] {slot['label']} prime trigger at "
                  f"pts={buf.pts / 1e9:.2f}s", file=sys.stderr)
            GLib.idle_add(prime_next)
            return Gst.PadProbeReturn.REMOVE
        return Gst.PadProbeReturn.OK
    probe_id = video_pad.add_probe(
        Gst.PadProbeType.BUFFER, _on_buffer
    )
    slot["prime_trigger_probe_id"] = probe_id
    return False


# --- Prime next clip into the OFF slot ----------------------------------

def prime_next():
    """Build the next playlist clip into the OFF slot, gate on its
    first frame, then start_fade. Returns False so callers via
    GLib.idle_add fire only once."""
    if fade_state["in_flight"]:
        print("[cutfade] prime_next called while fade in flight; "
              "skipping (next prime will fire on the new current)",
              file=sys.stderr)
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
    print(f"[cutfade] FADE complete; current is now slot "
          f"{incoming_idx}; retiring slot {outgoing_idx}",
          file=sys.stderr)
    # Retire outgoing on a low-priority idle so the heavy NULL
    # teardown does not compete with the new active stream's
    # cold-start moment. Positional form (priority, callable,
    # *args) is binding-version-portable; the `priority=` kwarg
    # form is not.
    GLib.idle_add(GLib.PRIORITY_LOW, retire_slot, outgoing_idx)
    # Attach prime trigger to the new current.
    attach_prime_trigger(incoming_idx)


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
    line = (
        f"[fps] t={uptime} "
        f"screen={screen_state['frames']} "
        f"max_gap_ms={int(screen_state['max_gap_ms'])} "
        f"cur={cur_label}(slot{cur}) "
        f"off={off_label}(slot{off}) "
        f"fade={fade_str} "
        f"gap_warns_total={screen_state['boundary_warns_total']}"
    )
    print(line, file=sys.stderr, flush=True)
    if log_file is not None:
        try:
            log_file.write(line + "\n")
        except OSError:
            pass
    # Watchdog: 0 frames for 2 consecutive ticks -> force prime
    if screen_state["frames"] == 0:
        watchdog_state["zero_frame_seconds"] += 1
        if watchdog_state["zero_frame_seconds"] >= WATCHDOG_STALL_S:
            print("[cutfade] WATCHDOG: 0 frames for 2s -- "
                  "force prime + re-PLAY", file=sys.stderr)
            watchdog_state["zero_frame_seconds"] = 0
            GLib.idle_add(prime_next)
            GLib.idle_add(
                lambda: pipeline.set_state(Gst.State.PLAYING) or False
            )
    else:
        watchdog_state["zero_frame_seconds"] = 0
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
