#!/usr/bin/env python3
"""fresh/cutfade.py -- 1s GL crossfade between two clips. POC.

Per qarl + QA dispatch (2026-06-18): revive the 1s crossfade in the
NEW Python clean-room renderer (cutloop). NOT touching the old Rust
renderer (qarl explicit: clean-room means start over). NOT modifying
cutloop.py (production-deployed on the sign right now); cutfade.py
is the experimental sibling so the production cutloop stays the
proven baseline.

ARCHITECTURE per QA dispatch:
  - Two v4l2h264dec instances during the 1s crossfade window only.
    Steady state is still ONE decoder (cutloop's premise preserved).
  - GL blend on V3D, NOT CPU compositor (which we measured ~14fps).
    glvideomixer + glimagesink.
  - Incoming decoder spawned 1-2s EARLY (off the hot path) so its
    cold-start completes before the fade window opens.
  - First-frame GATE on the incoming decoder: alpha animation only
    starts after the gate passes (BUFFER probe on dec_b.src). If the
    gate misses its deadline, hold the outgoing's last frame visible
    until the incoming is ready.
  - Cap concurrent priming to 1-2 (in this POC only ever 2; built
    in to the design).
  - Pools MODEST (no max-size-buffers boost; the prior dual-decode
    starvation work suggested deeper pools, but per QA dispatch
    that worsens swap).

POC SCOPE (THIS COMMIT):
  - Plays VIDEOS[0] on decA -> mix.sink_0 -> glimagesink (alpha 1).
  - At PRIME_LEAD_S into VIDEOS[0], spawns VIDEOS[1] on decB ->
    mix.sink_1 (alpha 0), PAUSED -> PLAYING when the GL pipeline
    is ready.
  - Gate on decB first frame (BUFFER probe on decB.src). Once gated,
    schedule the 1s alpha fade.
  - Fade alpha sink_0 1->0 and sink_1 0->1 over FADE_S=1s.
  - After fade: both stay alive (POC is one-shot, not looping).

NOT in this POC (follow-ups if QA likes the architecture):
  - Playlist cycling across 17 clips (currently hardcoded to first 2).
  - glob discovery.
  - Retire of the outgoing path post-fade.
  - Watchdog.
  - Re-prime cycle for the NEXT crossfade.

GL HEADLESS SYSTEMD CAVEAT: glimagesink + GST_GL_WINDOW=gbm has
historically hung at READY->PAUSED as a transient systemd unit (per
wipe_cpu.py earlier work). The XDG_RUNTIME_DIR fallback below
mitigates one known cause (logind not allocating /run/user/<uid>).
If the glimagesink path still hangs on first soak, set the env var
OPENMARQUEE_CUTFADE_SINK=kmssink to fall back to gldownload+
videoconvert+kmssink (the wipe_cpu.py path that did eventually work
under systemd once we sorted the env).
"""

import os
import signal
import subprocess
import sys
import time

# GL env vars MUST be set before Gst.init so gstreamer-gl picks the
# headless GBM/EGL backend on dri/card0 (no X/Wayland needed).
os.environ.setdefault("GST_GL_PLATFORM", "egl")
os.environ.setdefault("GST_GL_WINDOW", "gbm")
os.environ.setdefault("GST_GL_API", "gles2")


def _ensure_xdg_runtime_dir():
    """systemd transient units launched without logind get no
    XDG_RUNTIME_DIR. Mesa vc4 EGL/GBM uses it for dri socket path
    and shader cache discovery; without it the first GL context
    cold-compiles every shader -> PAUSED preroll past 10s. Per
    wipe_cpu.py earlier work."""
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


VIDEOS = [
    "/var/openmarquee/content/029c4d68-744c-4d30-9adc-0f37c55514f1/asset.mp4",
    "/var/openmarquee/content/3f54a4d2-a120-4c0c-aa80-5b99aaf7c9ff/asset.mp4",
]

# Timing per QA dispatch.
PRIME_LEAD_S = 1.5     # spawn decB at this many s after decA starts
FADE_S = 1.0           # crossfade duration
FADE_TICK_MS = 33      # animation tick (~30fps geometry update)
GATE_DEADLINE_MS = 2000  # max wait for decB first frame before
                          # falling back to hold-current-last-frame
PREROLL_BUDGET_S = 30  # cold-start headroom under systemd

# Output sink choice. Defaulting to kmssink per subagent review:
# glimagesink + GBM has a hang-at-READY-to-PAUSED history on this
# Pi build (wipe_cpu.py struggled with the same configuration).
# wipe_cpu.py's working path under systemd was glvideomixer ->
# gldownload -> videoconvert -> NV12 -> kmssink. Flip via env
# OPENMARQUEE_CUTFADE_SINK=glimagesink to try the direct GL sink
# (may still be the better path if it prerolls; flip and test).
SINK_CHOICE = os.environ.get(
    "OPENMARQUEE_CUTFADE_SINK", "kmssink"
).lower()


def die(msg, code=1):
    print(f"[cutfade] {msg}", file=sys.stderr)
    sys.exit(code)


# --- Pre-flights --------------------------------------------------------

for v in VIDEOS:
    if not os.path.exists(v):
        die(f"missing video: {v}")

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

# Single HW decoder context check. Two are expected DURING the
# fade window only; outside that window the production cutloop /
# mini may still hold one. Refuse double-start vs production.
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

# Output: glvideomixer -> (glimagesink | gldownload + videoconvert
# + kmssink).
mix = Gst.ElementFactory.make("glvideomixer", "mix")
mix.set_property("background", 1)  # 1 = black

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
          "videoconvert -> kmssink",
          file=sys.stderr)


def build_decode_path(clip_idx, label):
    """One filesrc -> qtdemux -> h264parse -> v4l2h264dec -> glupload
    sub-bin with a ghost src pad. Caller links the ghost into a
    glvideomixer request pad. Returns (sub_bin, decoder)."""
    asset = VIDEOS[clip_idx]
    sub = Gst.Bin.new(f"dec_{label}")
    filesrc = Gst.ElementFactory.make("filesrc", None)
    filesrc.set_property("location", asset)
    qtdemux = Gst.ElementFactory.make("qtdemux", None)
    h264parse = Gst.ElementFactory.make("h264parse", None)
    h264parse.set_property("config-interval", -1)
    decoder = Gst.ElementFactory.make("v4l2h264dec", f"dec{label}")
    glupload = Gst.ElementFactory.make("glupload", None)
    for el in (filesrc, qtdemux, h264parse, decoder, glupload):
        if el is None:
            die(f"[{label}] factory.make returned None")
        sub.add(el)

    if not filesrc.link(qtdemux):
        die(f"[{label}] filesrc -> qtdemux link failed")
    # qtdemux dynamic pad
    def _pad_added(_demux, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        caps_str = caps.to_string() if caps else ""
        if not caps_str.startswith("video/"):
            return
        sink_pad = h264parse.get_static_pad("sink")
        if sink_pad and not sink_pad.is_linked():
            pad.link(sink_pad)
    qtdemux.connect("pad-added", _pad_added)

    if not h264parse.link(decoder):
        die(f"[{label}] h264parse -> v4l2h264dec link failed")
    if not decoder.link(glupload):
        die(f"[{label}] v4l2h264dec -> glupload link failed")

    glupload_src = glupload.get_static_pad("src")
    ghost = Gst.GhostPad.new("src", glupload_src)
    sub.add_pad(ghost)
    return sub, decoder


# Path A: active from t=0.
sub_a, dec_a = build_decode_path(0, "A")
pipeline.add(sub_a)
mix_sink_a = mix.request_pad_simple("sink_%u")
if mix_sink_a is None:
    die("mix.request_pad_simple failed for A")
ghost_a = sub_a.get_static_pad("src")
if ghost_a.link(mix_sink_a) != Gst.PadLinkReturn.OK:
    die("A: ghost -> mix.sink link failed")
mix_sink_a.set_property("alpha", 1.0)
print("[cutfade] path A built + linked + alpha=1.0", file=sys.stderr)

# Path B: built later via prime_incoming().
sub_b = None
dec_b = None
mix_sink_b = None
gate_state = {"first_frame_seen": False, "deadline_at_ns": 0}


def prime_incoming():
    """Build + link + start playing path B (the incoming clip).
    Spawn a first-frame BUFFER probe on dec_b.src; once it fires,
    schedule the alpha fade."""
    global sub_b, dec_b, mix_sink_b
    print(f"[cutfade] prime incoming (clip {1})", file=sys.stderr)
    sub_b, dec_b = build_decode_path(1, "B")
    pipeline.add(sub_b)
    mix_sink_b = mix.request_pad_simple("sink_%u")
    if mix_sink_b is None:
        die("mix.request_pad_simple failed for B")
    ghost_b = sub_b.get_static_pad("src")
    if ghost_b.link(mix_sink_b) != Gst.PadLinkReturn.OK:
        die("B: ghost -> mix.sink link failed")
    mix_sink_b.set_property("alpha", 0.0)
    mix_sink_b.set_property("zorder", 1)  # B on top of A
    mix_sink_a.set_property("zorder", 0)

    # First-frame gate.
    dec_b_src = dec_b.get_static_pad("src")
    if dec_b_src is None:
        die("[B] v4l2h264dec has no src pad")

    def _on_first_frame(_pad, _info):
        if not gate_state["first_frame_seen"]:
            gate_state["first_frame_seen"] = True
            print("[cutfade] decB first frame -> ready, scheduling fade",
                  file=sys.stderr)
            GLib.idle_add(start_fade)
            return Gst.PadProbeReturn.REMOVE
        return Gst.PadProbeReturn.OK
    dec_b_src.add_probe(Gst.PadProbeType.BUFFER, _on_first_frame)

    # Deadline + fallback: if no first frame within GATE_DEADLINE_MS,
    # fall back to "hold-last-frame" semantics (just fade anyway;
    # glvideomixer holds the last-presented frame on a stalled pad
    # by default, so the visual is the last A frame held while B
    # comes up).
    gate_state["deadline_at_ns"] = (
        GLib.get_monotonic_time() * 1000
        + GATE_DEADLINE_MS * 1_000_000
    )
    GLib.timeout_add(GATE_DEADLINE_MS, _gate_deadline_check)

    sub_b.sync_state_with_parent()
    if sub_b.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        die("[B] sub_b set_state PLAYING failed")
    return False  # one-shot from GLib.timeout_add


def _gate_deadline_check():
    if gate_state["first_frame_seen"]:
        return False  # gate already passed; nothing to do
    print(f"[cutfade] GATE DEADLINE ({GATE_DEADLINE_MS}ms) -- "
          "decB never produced a frame; starting fade anyway "
          "(hold-last-frame fallback)", file=sys.stderr)
    start_fade()
    return False


fade_state = {"start_ns": 0, "in_flight": False, "done": False}


def start_fade():
    if fade_state["in_flight"] or fade_state["done"]:
        return False
    fade_state["in_flight"] = True
    fade_state["start_ns"] = GLib.get_monotonic_time() * 1000
    print("[cutfade] FADE start (1s)", file=sys.stderr)
    GLib.timeout_add(FADE_TICK_MS, fade_tick)
    return False


def fade_tick():
    elapsed_s = (
        (GLib.get_monotonic_time() * 1000 - fade_state["start_ns"]) / 1e9
    )
    if elapsed_s >= FADE_S:
        mix_sink_a.set_property("alpha", 0.0)
        mix_sink_b.set_property("alpha", 1.0)
        fade_state["in_flight"] = False
        fade_state["done"] = True
        print("[cutfade] FADE complete (alpha_a=0, alpha_b=1)",
              file=sys.stderr)
        return False
    t = elapsed_s / FADE_S
    mix_sink_a.set_property("alpha", 1.0 - t)
    mix_sink_b.set_property("alpha", t)
    return True


# --- Instrument (minimal) -----------------------------------------------

screen_state = {"frames": 0, "last_ns": 0, "max_gap_ms": 0.0}


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
        screen_state["last_ns"] = now
        return Gst.PadProbeReturn.OK
    sink_pad.add_probe(Gst.PadProbeType.BUFFER, _probe)


_t_start = time.monotonic()


def fps_tick():
    uptime = int(time.monotonic() - _t_start)
    line = (
        f"[fps] t={uptime} "
        f"screen={screen_state['frames']} "
        f"max_gap_ms={int(screen_state['max_gap_ms'])} "
        f"gate={'yes' if gate_state['first_frame_seen'] else 'no'} "
        f"fade={'done' if fade_state['done'] else ('in_flight' if fade_state['in_flight'] else 'pending')}"
    )
    print(line, file=sys.stderr, flush=True)
    screen_state["frames"] = 0
    screen_state["max_gap_ms"] = 0.0
    return True


# --- Bus + signals + run -----------------------------------------------

loop = GLib.MainLoop()


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
        print("[cutfade] pipeline EOS (one-shot POC ends here)",
              file=sys.stderr)
        loop.quit()


bus = pipeline.get_bus()
bus.add_signal_watch()
bus.connect("message", on_bus)


def shutdown(*_):
    GLib.idle_add(loop.quit)


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

print("==RUN-START== cutfade POC", file=sys.stderr)
print(f"[cutfade] PRIME_LEAD_S={PRIME_LEAD_S} FADE_S={FADE_S} "
      f"GATE_DEADLINE_MS={GATE_DEADLINE_MS} sink={SINK_CHOICE}",
      file=sys.stderr)

# Preroll the pipeline.
if pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
    die("set_state PAUSED failed")
# Poll for preroll with budget (GL cold-start under systemd can take
# 10-20s).
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
GLib.timeout_add(1000, fps_tick)
# Schedule the prime to fire PRIME_LEAD_S into A's playback.
GLib.timeout_add(int(PRIME_LEAD_S * 1000), prime_incoming)

try:
    loop.run()
finally:
    print("[cutfade] shutdown -> NULL", file=sys.stderr)
    pipeline.set_state(Gst.State.NULL)
