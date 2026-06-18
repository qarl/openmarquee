#!/usr/bin/env python3
"""fresh/xfade_reel.py -- 2-stream crossfade reel (the proven path).

Per QA dispatch 2026-06-18 after the cutfade dead-end + dissolve
proof: build the real crossfade-reel building block by COMBINING
the two glass-verified proven pieces:

  1. cutloop.py's concat-fed long-lived v4l2h264dec with the
     leak-safe add-next + retire-subgraph pattern. A SINGLE
     decoder per stream cycles many clips seamlessly; concat
     dynamic sink pads + disconnect-handlers-before-NULL +
     remove-probes-before-NULL prevent the closure leak. Used
     here PER STREAM.

  2. /usr/local/bin/xfade_demo.py (deployed on the sign):
     TWO PERMANENT glvideomixer pads, BOTH always-fed by two
     continuous v4l2h264dec, glimagesink, alpha animated --
     ran 262 loops at 24fps with no jam, CMA fine. That's the
     proven crossfade core.

EXPLICITLY NOT cutfade. cutfade's glvideomixer PAD-CHURN /
re-prime / EOS-to-pad / FLUSH-revive lifecycle is a confirmed
dead end (the aggregator running-time mismatch starves it
after re-prime; 8701765 instrument proved mix_out=0 with
pads non-EOS). NONE of that here:
  - The 2 mix sink pads are requested ONCE at startup.
  - NEVER released, NEVER flushed, NEVER re-primed, NEVER
    EOS-sent to. Only their ALPHA (and zorder) is ever
    changed.
  - BOTH streams ALWAYS feeding via concat so the mixer
    NEVER starves (an unfed pad is what we keep hitting).

HARD CEILING this Pi was confirmed to have: exactly 2
simultaneous v4l2h264dec instances. 3 = black (mixer stalls
on the 3rd). So this design uses EXACTLY 2 decoders, one
per stream. Never more.

ARCHITECTURE:

  PLAYLIST = [clip0, clip1, clip2]   (extend trivially to 17)

  Stream A: assigned indices 0, 2, 4, ... -> clip0, clip2,
            clip1, clip0, ... (mod 3)
  Stream B: assigned indices 1, 3, 5, ... -> clip1, clip0,
            clip2, clip1, ... (mod 3)

  Per stream: filesrc-clipN ! qtdemux ! h264parse ---+
              filesrc-clipM ! qtdemux ! h264parse ---+--> concat
              (added back-to-back by cutloop          (per stream)
               add_next_clip, retired by                |
               retire_subgraph on EOS)                  v
                                                    v4l2h264dec
                                                       (per stream)
                                                        |
                                                        v
                                                    glupload
                                                       (per stream)
                                                        |
                                                        v
                                                    mix.sink_0    (stream A, permanent)
                                                    mix.sink_1    (stream B, permanent)
                                                        |
                                                        v
                                                glvideomixer mix
                                                        |
                                                        v
                                                glimagesink sync=false

CROSSFADE SCHEDULING (v1: time-based, deterministic):
  visible_stream starts as "A" with alpha=1.0; B alpha=0.0.
  TARGET_VISIBLE_S=4.0 wall-clock after each crossfade
  completes, schedule the next crossfade. Crossfade animates
  the two alphas over FADE_S=1.0 (linear). When done, swap
  visible_stream and schedule the next TARGET_VISIBLE_S
  timer. The HIDDEN stream keeps playing its concat (its
  v4l2h264dec produces frames continuously); its current
  clip drifts independently of when it's shown. QA accepts
  this drift for v1: "If drift looks bad, a v2 refinement is
  to time the incoming clip's start to the crossfade -- but
  DON'T let an incoming stream go unfed to achieve that; the
  pad must always have buffers." This v1 always feeds.

OBSERVABILITY: 1Hz [fps] line:
  [fps] t=N screen=N max_gap_ms=N visible=X alphaA=. alphaB=.
        live_A=N live_B=N queued_A=N queued_B=N
        CmaFree_kB=N

GATE: all 3 clips cycle with a visible crossfade at each
boundary, screen ~24 sustained, CmaFree>50MB, NO black/
stall/jam over minutes.

DO NOT touch cutloop.py, cutfade.py, glblend_probe.py, or
dissolve_proof.py. cutloop stays the sign default; QA
deploys + runs this via a transient unit, then restores
cutloop.
"""

import gc
import os
import sys
import time
import weakref

os.environ.setdefault("GST_GL_PLATFORM", "egl")
os.environ.setdefault("GST_GL_WINDOW", "gbm")
os.environ.setdefault("GST_GL_API", "gles2")


def _ensure_xdg_runtime_dir():
    """systemd transient units launched without logind get no
    XDG_RUNTIME_DIR. Mesa vc4 EGL/GBM uses it; cold-compile
    blows preroll past 10s without it. Mirrors cutloop/cutfade."""
    if os.environ.get("XDG_RUNTIME_DIR"):
        return
    uid = os.getuid()
    for candidate in (f"/run/user/{uid}", f"/tmp/runtime-{uid}"):
        try:
            os.makedirs(candidate, mode=0o700, exist_ok=True)
            os.chmod(candidate, 0o700)
            os.environ["XDG_RUNTIME_DIR"] = candidate
            print(f"[xfade] XDG_RUNTIME_DIR fallback: {candidate}",
                  file=sys.stderr)
            return
        except OSError:
            continue


_ensure_xdg_runtime_dir()


import gi  # noqa: E402

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)
print("[xfade] Gst initialized", file=sys.stderr)


# --- Config ----------------------------------------------------------

PLAYLIST = [
    "/var/openmarquee/content/"
    "0ef82ed1-3699-4cd8-9c70-bdb8d4752e1e/asset.mp4",
    "/var/openmarquee/content/"
    "779d99e5-9742-4412-9025-f60187477bd2/asset.mp4",
    "/var/openmarquee/content/"
    "86b3eba8-3063-4c21-a2b1-749f7665e4d3/asset.mp4",
]

TARGET_VISIBLE_S = 4.0   # wall-clock visible time before next fade
FADE_S = 1.0             # crossfade duration
FADE_TICK_MS = 33        # ~30Hz alpha animation
PREROLL_BUDGET_S = 30
# cutloop's invariant: keep >=1 pad pending ahead of current,
# per stream. Pre-queue 2 sub-bins per stream at startup; on
# each EOS the probe schedules add_next_clip so the queue
# stays at >=1 pending at all times. Steady state: 2 sub-bins
# alive per stream (one active + one pending) = 4 sub-bins
# pipeline-wide. Decoder count is still 2 (one per stream,
# permanent).
INITIAL_QUEUE_DEPTH = 2
# Per QA cf992e4 CMA peak fix: defer the next add_next_clip
# scheduling until ADD_AFTER_RETIRE_MS after retire_subgraph
# has run. retire's sub_bin.set_state(NULL) returns
# synchronously for most elements but the streaming-thread
# tail (V4L2 STREAMOFF, GL buffer pool drain, etc.) may take
# milliseconds; allocating the next sub-bin immediately
# creates an overlap window where old+new resources are held.
# 50ms is comfortably longer than the typical tail and
# negligible vs the multi-second clip durations.
ADD_AFTER_RETIRE_MS = 50


def die(msg, code=1):
    print(f"[xfade] FATAL {msg}", file=sys.stderr)
    sys.exit(code)


for p in PLAYLIST:
    if not os.path.isfile(p):
        die(f"clip not found: {p}")
print(f"[xfade] PLAYLIST: {len(PLAYLIST)} clips, "
      f"TARGET_VISIBLE_S={TARGET_VISIBLE_S} FADE_S={FADE_S}",
      file=sys.stderr)


# --- Pipeline --------------------------------------------------------

pipeline = Gst.Pipeline.new("xfade_reel")
if pipeline is None:
    die("Gst.Pipeline.new returned None")

mix = Gst.ElementFactory.make("glvideomixer", "mix")
sink = Gst.ElementFactory.make("glimagesink", "sink")
if mix is None:
    die("glvideomixer factory returned None")
if sink is None:
    die("glimagesink factory returned None")
sink.set_property("sync", False)
for el in (mix, sink):
    pipeline.add(el)
if not mix.link(sink):
    die("mix -> sink link failed")


def _build_stream(label, mix_sink_idx):
    """Build a stream's static spine: concat -> v4l2h264dec ->
    queue (cap 2) -> glupload -> mix.sink_<mix_sink_idx>.
    Returns the dict of the static elements + the PERMANENT mix
    sink pad. Per-clip sub-bins are added/retired into concat
    by add_next_clip / retire_subgraph (cutloop pattern).

    Per QA cf992e4 CMA peak fix: a small queue with
    max-size-buffers=2 between dec and glupload caps the depth
    of decoded-frame refs held downstream. Without it,
    glupload's default-pool depth is uncapped and the
    v4l2h264dec CAPTURE pool stays pinned longer than
    necessary, contributing to the per-cycle CMA dip we saw
    (43-47MB low, 99MB high). Capping the queue lets the
    decoder release CAPTURE buffers as soon as glupload's
    GL-upload chain absorbs them."""
    concat = Gst.ElementFactory.make("concat", f"concat_{label}")
    dec = Gst.ElementFactory.make("v4l2h264dec", f"dec_{label}")
    queue = Gst.ElementFactory.make("queue", f"outq_{label}")
    upl = Gst.ElementFactory.make("glupload", f"upl_{label}")
    if concat is None or dec is None or queue is None or upl is None:
        die(f"[{label}] core static element factory returned None")
    # Cap downstream depth. max-size-bytes/time = 0 disables those
    # backpressure axes; max-size-buffers=2 keeps the queue at
    # most 2 decoded frames behind glupload.
    queue.set_property("max-size-buffers", 2)
    queue.set_property("max-size-bytes", 0)
    queue.set_property("max-size-time", 0)
    for el in (concat, dec, queue, upl):
        pipeline.add(el)
    if not concat.link(dec):
        die(f"[{label}] concat -> dec link failed")
    if not dec.link(queue):
        die(f"[{label}] dec -> queue link failed")
    if not queue.link(upl):
        die(f"[{label}] queue -> upl link failed")
    mix_pad = mix.request_pad_simple("sink_%u")
    if mix_pad is None:
        die(f"[{label}] mix.request_pad_simple failed")
    # PERMANENT mix sink pad. NEVER released, NEVER flushed,
    # NEVER re-primed, NEVER EOS-sent-to. Only alpha + zorder
    # change ever. This is what avoids every cutfade wedge.
    mix_pad.set_property("zorder", mix_sink_idx)
    upl_src = upl.get_static_pad("src")
    if upl_src is None:
        die(f"[{label}] glupload has no src pad")
    if upl_src.link(mix_pad) != Gst.PadLinkReturn.OK:
        die(f"[{label}] upload.src -> mix.sink link failed")
    return {"label": label, "concat": concat, "dec": dec,
            "queue": queue, "upl": upl, "mix_pad": mix_pad}


# Build both streams BEFORE setting alphas so the link order is
# deterministic. Stream A on mix.sink_0 (zorder 0); stream B on
# mix.sink_1 (zorder 1 = on top).
streams = {
    "A": _build_stream("A", 0),
    "B": _build_stream("B", 1),
}
streams["A"]["mix_pad"].set_property("alpha", 1.0)
streams["B"]["mix_pad"].set_property("alpha", 0.0)
print("[xfade] both static spines built; permanent mix pads "
      "A.alpha=1.0 B.alpha=0.0", file=sys.stderr)


# --- Per-stream playlist + cutloop-style add-next/retire -------------
#
# Each stream maintains its own playlist-cycle counter. Stream A
# starts at next_idx=0, B at next_idx=1; each bumps by 2 per add.
# Mod len(PLAYLIST) wraps the cycle.

for sid, s in streams.items():
    s["next_idx"] = 0 if sid == "A" else 1
    s["playlist_added_count"] = 0
    s["live_subgraph_count"] = 0


def add_next_clip(stream_id):
    """Append the next assigned playlist clip to this stream's
    concat as a new sub-bin. cutloop pattern verbatim, scoped to
    the per-stream concat. Called from main thread (initial loop
    + GLib.idle_add from EOS probe on prior sub-bin)."""
    s = streams[stream_id]
    concat = s["concat"]
    clip_idx = s["next_idx"] % len(PLAYLIST)
    asset = PLAYLIST[clip_idx]
    s["next_idx"] += 2  # alternating playlist stride
    serial = s["playlist_added_count"]
    s["playlist_added_count"] += 1
    asset_name = os.path.basename(os.path.dirname(asset)) or asset

    sub = Gst.Bin.new(f"src_{stream_id}_{serial}")
    filesrc = Gst.ElementFactory.make("filesrc", None)
    filesrc.set_property("location", asset)
    qtdemux = Gst.ElementFactory.make("qtdemux", None)
    h264parse = Gst.ElementFactory.make("h264parse", None)
    h264parse.set_property("config-interval", -1)
    for el in (filesrc, qtdemux, h264parse):
        if el is None:
            print(f"[xfade] [{stream_id}/{serial}] factory.make "
                  "failed -- quitting", file=sys.stderr)
            loop.quit()
            return False
        sub.add(el)
    if not filesrc.link(qtdemux):
        print(f"[xfade] [{stream_id}/{serial}] filesrc -> qtdemux "
              "link failed -- quitting", file=sys.stderr)
        loop.quit()
        return False

    def _on_pad_added(_demux, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        caps_str = caps.to_string() if caps else ""
        if not caps_str.startswith("video/"):
            return
        sink_pad = h264parse.get_static_pad("sink")
        if sink_pad is None or sink_pad.is_linked():
            return
        pad.link(sink_pad)
    # STORE handler id so retire can disconnect later. Per
    # cutloop leak fix.
    pad_added_id = qtdemux.connect("pad-added", _on_pad_added)

    h264_src = h264parse.get_static_pad("src")
    ghost = Gst.GhostPad.new("src", h264_src)
    sub.add_pad(ghost)
    pipeline.add(sub)

    concat_sink = concat.request_pad_simple("sink_%u")
    if concat_sink is None:
        print(f"[xfade] [{stream_id}/{serial}] concat sink "
              "request failed -- quitting", file=sys.stderr)
        loop.quit()
        return False
    if ghost.link(concat_sink) != Gst.PadLinkReturn.OK:
        print(f"[xfade] [{stream_id}/{serial}] ghost -> "
              "concat.sink link failed -- quitting",
              file=sys.stderr)
        loop.quit()
        return False

    # EOS probe on the CONCAT SINK pad (cutloop pattern). When
    # concat sees EOS on this sink pad, it switches to the next
    # pending sink pad and absorbs the EOS internally -- no
    # downstream EOS, no STREAMOFF on the decoder. Schedule
    # retire + queue next on main-thread idle.
    eos_probe_id = None

    def _on_concat_sink_event(_pad, info):
        ev = info.get_event()
        if ev and ev.type == Gst.EventType.EOS:
            print(f"[xfade] {stream_id} concat EOS bin {serial} "
                  f"({asset_name}) -> retire + queue next",
                  file=sys.stderr)
            # Schedule retire only here. retire_subgraph itself
            # schedules add_next_clip with a SHORT delay so the
            # streaming thread can finish the NULL transition on
            # this just-retired bin before the new bin allocates
            # (per QA cf992e4 CMA peak fix: tighten retire-before-
            # add ordering to drop the per-cycle overlap window).
            GLib.idle_add(retire_subgraph, stream_id, sub,
                          concat_sink, qtdemux, pad_added_id,
                          eos_probe_id)
        return Gst.PadProbeReturn.OK
    eos_probe_id = concat_sink.add_probe(
        Gst.PadProbeType.EVENT_DOWNSTREAM, _on_concat_sink_event
    )

    sub.sync_state_with_parent()
    s["live_subgraph_count"] += 1
    print(f"[xfade] queued {stream_id} bin {serial} = "
          f"{asset_name} (live_bins={s['live_subgraph_count']} "
          f"added={s['playlist_added_count']})",
          file=sys.stderr)
    return False  # one-shot when called via GLib.idle_add


def retire_subgraph(stream_id, sub_bin, concat_sink_pad,
                    qtdemux_elem, pad_added_id, eos_probe_id):
    """cutloop's proven leak-safe retire: disconnect handlers +
    remove probes BEFORE set_state(NULL) so the closures release
    the sub-bin's elements; then NULL + pipeline.remove +
    concat.release_request_pad. Scoped per stream."""
    s = streams[stream_id]
    name = sub_bin.get_name() if sub_bin else "?"
    weak = weakref.ref(sub_bin)
    try:
        # (1) Disconnect closures FIRST.
        if pad_added_id:
            try:
                qtdemux_elem.disconnect(pad_added_id)
            except Exception as exc:
                print(f"[xfade] retire {name} pad-added "
                      f"disconnect WARN: {exc}", file=sys.stderr)
        if eos_probe_id:
            try:
                concat_sink_pad.remove_probe(eos_probe_id)
            except Exception as exc:
                print(f"[xfade] retire {name} probe remove "
                      f"WARN: {exc}", file=sys.stderr)
        # (2-4) Tear down + release.
        sub_bin.set_state(Gst.State.NULL)
        pipeline.remove(sub_bin)
        s["concat"].release_request_pad(concat_sink_pad)
        s["live_subgraph_count"] -= 1
        print(f"[xfade] retire {name} (live_bins_"
              f"{stream_id}={s['live_subgraph_count']})",
              file=sys.stderr)
    except Exception as exc:
        print(f"[xfade] retire {name} WARN: {exc}",
              file=sys.stderr)
    del sub_bin, concat_sink_pad, qtdemux_elem

    def _check_leak():
        gc.collect()
        if weak() is not None:
            print(f"[xfade] [retire] LEAK ref still held for "
                  f"{name}", file=sys.stderr)
        return False
    GLib.idle_add(_check_leak)

    # Per QA cf992e4 CMA peak fix: defer the next add by
    # ADD_AFTER_RETIRE_MS so the streaming thread has time to
    # finish the NULL transition on this just-retired bin
    # before the new bin starts allocating. Tightens the
    # per-cycle overlap where old+new resources are
    # simultaneously held. Concat starvation risk is
    # negligible: INITIAL_QUEUE_DEPTH=2 means at this point
    # there is still 1 active sub-bin feeding concat; the
    # delay only briefly drops the pending count from 1 to 0.
    GLib.timeout_add(ADD_AFTER_RETIRE_MS, add_next_clip,
                     stream_id)
    return False  # one-shot


# Pre-queue INITIAL_QUEUE_DEPTH clips per stream BEFORE preroll.
for sid in ("A", "B"):
    for _ in range(INITIAL_QUEUE_DEPTH):
        add_next_clip(sid)


# --- Crossfade scheduler --------------------------------------------
#
# Time-based: visible_stream alpha 1.0 -> 0.0 and hidden_stream
# 0.0 -> 1.0 over FADE_S linear. v1 simplicity; QA explicit on
# accepting drift for v1.

visible_stream = ["A"]
fade_state = {
    "in_flight": False,
    "start_ns": 0,
    "from_stream": None,
    "to_stream": None,
}


def _start_crossfade():
    if fade_state["in_flight"]:
        return False
    from_id = visible_stream[0]
    to_id = "B" if from_id == "A" else "A"
    fade_state["in_flight"] = True
    fade_state["start_ns"] = GLib.get_monotonic_time() * 1000
    fade_state["from_stream"] = from_id
    fade_state["to_stream"] = to_id
    print(f"[xfade] FADE start: {from_id} -> {to_id}",
          file=sys.stderr)
    GLib.timeout_add(FADE_TICK_MS, _fade_tick)
    return False


def _fade_tick():
    if not fade_state["in_flight"]:
        return False
    from_id = fade_state["from_stream"]
    to_id = fade_state["to_stream"]
    from_pad = streams[from_id]["mix_pad"]
    to_pad = streams[to_id]["mix_pad"]
    now_ns = GLib.get_monotonic_time() * 1000
    elapsed_s = (now_ns - fade_state["start_ns"]) / 1e9
    if elapsed_s >= FADE_S:
        from_pad.set_property("alpha", 0.0)
        to_pad.set_property("alpha", 1.0)
        visible_stream[0] = to_id
        fade_state["in_flight"] = False
        print(f"[xfade] FADE complete; visible={to_id}; "
              f"next fade in {TARGET_VISIBLE_S}s",
              file=sys.stderr)
        GLib.timeout_add(int(TARGET_VISIBLE_S * 1000),
                         _start_crossfade)
        return False
    t = max(0.0, min(1.0, elapsed_s / FADE_S))
    from_pad.set_property("alpha", 1.0 - t)
    to_pad.set_property("alpha", t)
    return True


# --- Observability + bus ---------------------------------------------

screen_state = {"frames": 0, "last_ns": 0, "max_gap_ms": 0.0}

sink_pad_for_probe = sink.get_static_pad("sink")
if sink_pad_for_probe is None:
    die("glimagesink has no sink pad")


def _on_sink_buf(_pad, _info):
    now = time.monotonic_ns()
    screen_state["frames"] += 1
    last = screen_state["last_ns"]
    if last:
        gap_ms = (now - last) / 1e6
        if gap_ms > screen_state["max_gap_ms"]:
            screen_state["max_gap_ms"] = gap_ms
    screen_state["last_ns"] = now
    return Gst.PadProbeReturn.OK


sink_pad_for_probe.add_probe(
    Gst.PadProbeType.BUFFER, _on_sink_buf
)


def _cma_free_kb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("CmaFree:"):
                    return int(line.split()[1])
    except Exception:
        return -1
    return -1


_t_start = time.monotonic()


cma_state = {"min_kb": None}  # running min across the soak


def fps_tick():
    uptime = int(time.monotonic() - _t_start)
    vis = visible_stream[0]
    a_alpha = streams["A"]["mix_pad"].get_property("alpha")
    b_alpha = streams["B"]["mix_pad"].get_property("alpha")
    # Per QA cf992e4 dispatch: track + log CmaFree_min so the
    # per-cycle dip is visible in the soak log. The dip (not
    # the mean) is what bricks the Pi if it crosses the
    # CMA-exhaustion floor.
    cma_kb = _cma_free_kb()
    if cma_kb >= 0:
        if cma_state["min_kb"] is None or cma_kb < cma_state["min_kb"]:
            cma_state["min_kb"] = cma_kb
    cma_min_str = (str(cma_state["min_kb"])
                   if cma_state["min_kb"] is not None else "?")
    line = (
        f"[fps] t={uptime} "
        f"screen={screen_state['frames']} "
        f"max_gap_ms={int(screen_state['max_gap_ms'])} "
        f"visible={vis} "
        f"alphaA={a_alpha:.2f} alphaB={b_alpha:.2f} "
        f"live_A={streams['A']['live_subgraph_count']} "
        f"live_B={streams['B']['live_subgraph_count']} "
        f"added_A={streams['A']['playlist_added_count']} "
        f"added_B={streams['B']['playlist_added_count']} "
        f"CmaFree_kB={cma_kb} "
        f"CmaFree_min_kB={cma_min_str}"
    )
    print(line, file=sys.stderr, flush=True)
    screen_state["frames"] = 0
    screen_state["max_gap_ms"] = 0.0
    return True


loop = GLib.MainLoop()


def on_bus(_bus, msg):
    if msg.type == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        src = msg.src.get_name() if msg.src else "?"
        print(f"[xfade] ERROR {src}: {err.message}",
              file=sys.stderr)
        if dbg:
            print(f"[xfade]  debug: {dbg}", file=sys.stderr)
        loop.quit()
    elif msg.type == Gst.MessageType.EOS:
        # Should not happen: concat absorbs per-clip EOS
        # internally; no pipeline EOS is expected. If it
        # reaches the bus, log + quit.
        print("[xfade] pipeline EOS (unexpected)",
              file=sys.stderr)
        loop.quit()


bus = pipeline.get_bus()
bus.add_signal_watch()
bus.connect("message", on_bus)


import signal  # noqa: E402


def shutdown(*_):
    GLib.idle_add(loop.quit)


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)


# --- Run -------------------------------------------------------------

print("[xfade] PAUSED + preroll", file=sys.stderr)
if (pipeline.set_state(Gst.State.PAUSED)
        == Gst.StateChangeReturn.FAILURE):
    die("set_state PAUSED failed")
prerolled = False
for elapsed in range(1, PREROLL_BUDGET_S + 1):
    ret, cur, pending = pipeline.get_state(1 * Gst.SECOND)
    if ret == Gst.StateChangeReturn.SUCCESS:
        print(f"[xfade] preroll done after ~{elapsed}s "
              f"state={cur.value_nick}", file=sys.stderr)
        prerolled = True
        break
    if ret == Gst.StateChangeReturn.NO_PREROLL:
        print(f"[xfade] preroll NO_PREROLL after ~{elapsed}s "
              "(live source; accepting)", file=sys.stderr)
        prerolled = True
        break
    if ret == Gst.StateChangeReturn.FAILURE:
        die("preroll FAILURE")
    print(f"[xfade] preroll... state={cur.value_nick} "
          f"pending={pending.value_nick} "
          f"({elapsed}/{PREROLL_BUDGET_S}s)", file=sys.stderr)
if not prerolled:
    die(f"preroll exceeded {PREROLL_BUDGET_S}s budget")

print("[xfade] PLAYING", file=sys.stderr)
if (pipeline.set_state(Gst.State.PLAYING)
        == Gst.StateChangeReturn.FAILURE):
    die("set_state PLAYING failed")

print(f"[xfade] first crossfade in {TARGET_VISIBLE_S}s",
      file=sys.stderr)
GLib.timeout_add(int(TARGET_VISIBLE_S * 1000), _start_crossfade)
GLib.timeout_add(1000, fps_tick)


try:
    loop.run()
finally:
    print("[xfade] shutdown -> NULL", file=sys.stderr)
    pipeline.set_state(Gst.State.NULL)
    print("[xfade] done", file=sys.stderr)
