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

CROSSFADE SCHEDULING (v2: clip-gated linger, per QA glass):
  v1's fixed 4s TARGET_VISIBLE_S fired mid-clip and produced
  weird drift / cutting. v2 replaces it with a clip-gated
  linger: each visible clip plays FULL LENGTH AND >=
  LINGER_MIN_S (short clips LOOP via re-queue of the same
  clip). Crossfade fires ONLY at a clean boundary: the
  incoming stream is FORCE-ADVANCED (2x EOS to current active)
  to its next playlist clip so its first frame is FROM ITS
  START at the crossfade moment.

  STATE MACHINE:
    LINGERING_VISIBLE -- visible turn alive; polled every
        LINGER_CHECK_MS. When elapsed wall-clock since
        visible_turn_started >= max(clip_dur, LINGER_MIN_S),
        transition to ADVANCING_INCOMING.
    ADVANCING_INCOMING -- incoming.current_loop_clip reassigned
        to its next playlist entry; force_eos_active fired
        twice (with a small delay between) so concat advances
        pending -> new clip from frame 0. Wait
        ADVANCE_FIRST_FRAME_WAIT_MS for buffers to flow
        through dec -> queue -> glupload -> mix.
    FADING -- linear alpha animation over FADE_S. On
        completion: swap visible_stream, re-enter
        LINGERING_VISIBLE with new linger_target.

  LOOPING: each stream's add_next_clip queues a fresh sub-bin
  of current_loop_clip. On natural EOS the cutloop pattern
  retires + queues another instance of the same clip. The
  per-stream playlist index (next_idx) only advances inside
  _start_advancing, NOT on every EOS.

  PRESERVED FROM v1: both streams ALWAYS feeding via concat
  so the mixer never starves; 2 PERMANENT mix sink pads
  (never released / flushed / re-primed / EOS-sent to);
  cutloop's leak-safe retire (disconnect handlers + remove
  probes BEFORE NULL) per stream; the f3fd066 CMA peak fixes
  (queue cap 2 between dec and glupload, deferred add after
  retire by ADD_AFTER_RETIRE_MS=50ms, CmaFree_min logging).

OBSERVABILITY: 1Hz [fps] line:
  [fps] t=N screen=N max_gap_ms=N visible=X alphaA=. alphaB=.
        phase=PHASE linger_rem=N.Ns live_A=N live_B=N
        added_A=N added_B=N CmaFree_kB=N CmaFree_min_kB=N

GATE per QA v2 dispatch: all 3 clips show in clean rotation
(each visible clip starts from frame 0, plays full length AND
>=10s, then a 1s dissolve to the next fresh clip), screen
~24 sustained, CmaFree_min_kB > 55-60MB (raised from the v1
43-47MB dip), NO mid-clip flashing/cutting, NO black/stall/
jam over minutes.

DO NOT touch cutloop.py, cutfade.py, glblend_probe.py, or
dissolve_proof.py. cutloop stays the sign default; QA
deploys + runs this via a transient unit, then restores
cutloop.
"""

import gc
import os
import queue as _queue_module
import sys
import threading
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

# Per QA a84cee9 + qarl's firm 20s-per-video rule: each visible
# clip holds for max(clip_dur, LINGER_MIN_S). Short clips that
# would EOS before LINGER_MIN_S elapses are LOOPED via concat-
# re-add of the SAME current_loop_clip (cutloop's proven gapless
# mechanism: EAGER first-buffer probe on each sub-bin's concat
# sink pad triggers schedule_add when concat unblocks that pad
# = becomes active. The next instance of the same clip queues
# ahead of the current's natural EOS; concat advances pending ->
# next gaplessly). NO seek (a non-flushing SEGMENT seek on the
# bcm2835 V4L2 decoder is a proven HW dead end: visible stream
# freezes or SIGSEGVs -- two glass tests 2026-06-18). Retire on
# idle on EVERY EOS (cutloop pattern); no visible-vs-hidden
# defer (post-1c737bc).
LINGER_MIN_S = float(os.environ.get(
    "OPENMARQUEE_XFADE_LINGER_MIN_S", "20.0"
))
FADE_S = 1.0             # crossfade duration
FADE_TICK_MS = 33        # ~30Hz alpha animation
LINGER_CHECK_MS = 500    # how often to poll the linger gate
PREROLL_BUDGET_S = 30
# After force-advancing the incoming stream (2x force-EOS so
# concat advances pending -> new-clip), wait this long for the
# new clip's first frames to flow through dec -> queue ->
# glupload -> mix sink before starting the alpha animation.
# Tuned for bcm2835-codec first-frame latency (~0.5-1s).
ADVANCE_FIRST_FRAME_WAIT_MS = 800
# Per QA 7561fac soak: cross-stream retire collision is the
# CMA-floor risk. When A and B retire+add within ~50ms, two
# new sub-bins allocate concurrently and the per-cycle CMA
# dip doubles (observed: CmaFree_min 41.2MB below the 50MB
# brick floor on a 2nd live run). FIX: SERIALIZE add_next_clip
# calls across streams via a global next-allowed timestamp.
# Each scheduled add cannot run within MIN_INTER_ADD_MS of the
# previously-scheduled add (across BOTH streams). The
# allocation work itself is already on the GLib main loop
# (single-threaded), so the gap targets CMA-settle time
# rather than thread synchronization.
MIN_INTER_ADD_MS = 200
# Time-based queue between v4l2h264dec and glupload (env-tunable).
# Bridges the wrap underrun at concat sub-bin advances. Default
# 600ms (reverted from 446511d's 700ms experiment after the leaky=2
# regression made the bigger reservoir irrelevant). With glimagesink
# sync=true (set below) pacing the chain to steady 24fps and a
# non-leaky queue, dec is back-pressured to mixer rate; queue stays
# near cap and provides head-start for the wrap IDR-decode.
# CMA budget at 600ms: 1280x720 NV12 = 1.4MB/frame x 24fps x 0.6s
# = ~20MB per stream; 2 streams = ~40MB. Plus EAGER's +1 sub-bin
# per stream (~1-2MB each, filesrc+qtdemux+h264parse only). Combined
# projection: CmaFree_min ~58MB at steady, ~61MB at fade boundary
# (matches 8e2215e measured headroom).
# Env-tunable: shrink to ~300ms if sync=true pacing proves the big
# reservoir unneeded (cutloop ships with effective ~290ms and is
# smooth); grow only if hitches persist AND a soak shows headroom.
RESERVOIR_MS = int(os.environ.get(
    "OPENMARQUEE_XFADE_RESERVOIR_MS", "600"
))
RESERVOIR_TIME_NS = RESERVOIR_MS * 1_000_000
# Fallback per-clip duration if query_duration fails at startup.
DUR_FALLBACK_S = 6.0
# Pre-queue depth at startup. Pre-queue this many sub-bins per
# stream at startup. Once playing, the EAGER first-buffer probe
# on each sub-bin's concat sink pad schedules add_next_clip when
# concat unblocks that pad (= becomes active), keeping the queue
# one ahead of the current active. Steady state under EAGER:
# 3 sub-bins alive per stream (active + 2 pending) = 6 sub-bins
# pipeline-wide; brief 4 during fade-driven double-EOS. Decoder
# count is still 2 (one per stream, permanent in the static spine).
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
      f"LINGER_MIN_S={LINGER_MIN_S} FADE_S={FADE_S}",
      file=sys.stderr)


# --- Per-clip duration cache ----------------------------------------
#
# Query each clip's duration ONCE at startup via a transient parse
# pipeline. Cache results so the linger gate can compute hold_target
# = max(clip_dur, LINGER_MIN_S) without per-cycle querying.
CLIP_DURATIONS = {}


def _query_clip_duration(path):
    """One-shot duration query via a transient
    filesrc!qtdemux!h264parse!fakesink pipeline. Blocks up to 5s
    for PAUSED state, then queries qtdemux. Returns float seconds
    or DUR_FALLBACK_S on failure."""
    pipe = None
    try:
        pipe = Gst.parse_launch(
            f'filesrc location="{path}" ! qtdemux ! h264parse '
            "! fakesink name=fs"
        )
        if pipe is None:
            return DUR_FALLBACK_S
        if (pipe.set_state(Gst.State.PAUSED)
                == Gst.StateChangeReturn.FAILURE):
            return DUR_FALLBACK_S
        ret, _cur, _pending = pipe.get_state(5 * Gst.SECOND)
        if ret == Gst.StateChangeReturn.FAILURE:
            return DUR_FALLBACK_S
        ok, dur = pipe.query_duration(Gst.Format.TIME)
        if ok and dur > 0:
            return dur / 1e9
    except Exception as exc:
        print(f"[xfade] dur query WARN {path}: {exc}",
              file=sys.stderr)
    finally:
        if pipe is not None:
            pipe.set_state(Gst.State.NULL)
    return DUR_FALLBACK_S


for p in PLAYLIST:
    d = _query_clip_duration(p)
    CLIP_DURATIONS[p] = d
    name = os.path.basename(os.path.dirname(p)) or p
    print(f"[xfade] dur {name}: {d:.2f}s "
          f"(linger_target={max(d, LINGER_MIN_S):.2f}s)",
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
# sync=False (8e2215e baseline). 12458ae tried sync=True per the
# cutloop-pattern hypothesis; QA glass-soak showed it brought BACK
# the drain-crash at the first crossfade (sync pacing changes EOS
# propagation so the crossfade force-EOS path drains to pipeline-
# EOS even with the pre-fortify guard) AND was still hitchy (12%
# smooth). Reverted to 8e2215e behavior (our BEST so far: stable 8
# crossfades, 66% smooth, 264ms worst wrap-hitch).
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
    # Time-based queue (~600ms by default) between dec and
    # glupload. Bridges the wrap underrun at concat sub-bin
    # advances. max-size-bytes and max-size-buffers = 0
    # (disabled) so only time gates the cap. Non-leaky
    # (default): decoder fills queue to cap then back-pressures;
    # at wrap, dec briefly pauses, queue feeds mixer from its
    # buffered frames. 8e2215e baseline behavior.
    queue.set_property("max-size-time", RESERVOIR_TIME_NS)
    queue.set_property("max-size-bytes", 0)
    queue.set_property("max-size-buffers", 0)
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
    # next_idx walks the GLOBAL playlist in alternating stride.
    # A: 0, 2, 4, ... -> PLAYLIST[idx mod len] picks the per-A
    # clip; B: 1, 3, 5, ... similarly. Both reach all clips
    # without repeats over a full cycle (verified for len=3).
    s["next_idx"] = 0 if sid == "A" else 1
    s["playlist_added_count"] = 0
    s["live_subgraph_count"] = 0
    # current_loop_clip: the clip path being looped through this
    # stream's concat right now. add_next_clip uses this when no
    # explicit clip_path is passed. Updated only by the advance
    # path, which sets it to the new clip just before crossfade.
    s["current_loop_clip"] = PLAYLIST[s["next_idx"] % len(PLAYLIST)]
    # Bump next_idx past this initial assignment so the FIRST
    # advance picks the next clip in the per-stream walk.
    s["next_idx"] += 2
    # sub_bin_queue: list of (sub_bin, filesrc) tuples in
    # concat-input order. Front (idx 0) is the currently-active
    # source feeding concat; rest are pending. add_next_clip
    # appends; retire_subgraph pops front (the one that just
    # EOSed). force_eos_active reads queue[0]["filesrc"] and
    # sends EOS to it.
    s["sub_bin_queue"] = []
    # has_been_visible: True if this stream has been the visible
    # stream at any prior point. A is initialized True (it's
    # the startup-visible stream). B is False until the first
    # crossfade-to-B. _start_advancing reads this to decide
    # whether to advance the playlist on the incoming side:
    # on the incoming's FIRST crossfade-to-it, KEEP the
    # current_loop_clip (the stream was initialized with the
    # correct first clip and just needs a "fresh from frame 0"
    # restart via force-EOS). On subsequent crossfades-to-this-
    # stream, advance the playlist to the next assigned clip.
    # Without this gate, B's first crossfade would advance
    # B from clip1 to clip0, making the viewer see clip0 twice
    # in a row (A.clip0 then B.clip0). Reviewer's catch.
    s["has_been_visible"] = (sid == "A")
    # NOTE: pending_retires (visible-defer mechanism) was REMOVED
    # per QA c41cf01 soak: the visible-side defer caused live_<sid>
    # to climb to 7 over a 20s linger (~4-5 visible-clip loops, each
    # EOS'd sub-bin queued but never drained), exhausting CMA to
    # ~3MB free = near-brick. Retires now run unconditionally on
    # GLib.idle_add from the EOS probe (matches cutloop's shipped
    # pattern exactly). Safe because concat absorbs the EOS and
    # switches to the next pending sub-bin BEFORE the idle fires,
    # so the retiring sub-bin is no longer feeding the mix pad.


# Per QA 7561fac soak cross-stream collision fix: global
# next-allowed timestamp gates ALL deferred add_next_clip calls
# (across both streams) so two streams' retire-then-add cycles
# never run their allocations concurrently. Single int wrapped
# in a list for mutability from inside schedule_add.
_next_add_allowed_ns = [0]


def schedule_add(stream_id):
    """Schedule add_next_clip(stream_id) such that:
    - it runs at least ADD_AFTER_RETIRE_MS after retire fires
      (per-stream NULL-transition completion window), AND
    - it runs at least MIN_INTER_ADD_MS after the previous
      add was scheduled to run (cross-stream allocation
      serialization).

    The allocation work itself is already on the GLib main loop
    (single-threaded), so this serialization targets CMA-settle
    time rather than thread synchronization. Without it, A's
    and B's adds firing within ~50ms double the per-cycle CMA
    peak (QA 7561fac soak: CmaFree_min 41.2MB on a 2nd live
    run vs 51.6MB on the first -- the only difference was
    timing alignment of the two streams' boundaries)."""
    now_ns = GLib.get_monotonic_time() * 1000
    earliest_ns = max(
        now_ns + ADD_AFTER_RETIRE_MS * 1_000_000,
        _next_add_allowed_ns[0],
    )
    delay_ms = int(max(0, (earliest_ns - now_ns) // 1_000_000))
    # The NEXT scheduled add can't run until this one's
    # earliest + MIN_INTER_ADD_MS later.
    _next_add_allowed_ns[0] = (
        earliest_ns + MIN_INTER_ADD_MS * 1_000_000
    )
    if delay_ms > ADD_AFTER_RETIRE_MS + 10:
        # Logged only when serialization actually inserted
        # extra delay beyond the per-stream deferred-add
        # window (i.e. cross-stream collision was detected
        # and elided).
        print(f"[xfade] schedule_add {stream_id}: "
              f"serialized delay={delay_ms}ms "
              f"(cross-stream collision elided)",
              file=sys.stderr)
    GLib.timeout_add(delay_ms, add_next_clip, stream_id)


def add_next_clip(stream_id):
    """Append THIS STREAM'S current_loop_clip to its concat as a
    new sub-bin. Per QA dispatch the per-stream playlist no longer
    advances on every EOS -- the same loop clip is queued until
    the orchestrator advances current_loop_clip just before the
    crossfade. cutloop's add+retire pattern verbatim otherwise."""
    s = streams[stream_id]
    concat = s["concat"]
    asset = s["current_loop_clip"]
    serial = s["playlist_added_count"]
    s["playlist_added_count"] += 1
    asset_name = os.path.basename(os.path.dirname(asset)) or asset

    # Per QA 22382ad [stall] dispatch: time every main-thread op so
    # the [wrap-timing] log pins which one eats the 100-330ms.
    _wt_start = time.monotonic_ns()

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
    _wt_create = time.monotonic_ns()
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

    _wt_link = time.monotonic_ns()
    h264_src = h264parse.get_static_pad("src")
    ghost = Gst.GhostPad.new("src", h264_src)
    sub.add_pad(ghost)
    _wt_ghost = time.monotonic_ns()
    pipeline.add(sub)
    _wt_pipeadd = time.monotonic_ns()

    concat_sink = concat.request_pad_simple("sink_%u")
    if concat_sink is None:
        print(f"[xfade] [{stream_id}/{serial}] concat sink "
              "request failed -- quitting", file=sys.stderr)
        loop.quit()
        return False
    _wt_reqpad = time.monotonic_ns()
    if ghost.link(concat_sink) != Gst.PadLinkReturn.OK:
        print(f"[xfade] [{stream_id}/{serial}] ghost -> "
              "concat.sink link failed -- quitting",
              file=sys.stderr)
        loop.quit()
        return False
    _wt_padlink = time.monotonic_ns()

    # EOS probe on the CONCAT SINK pad (cutloop pattern). When
    # concat sees EOS on this sink pad, it switches to the next
    # pending sink pad and absorbs the EOS internally -- no
    # downstream EOS, no STREAMOFF on the decoder. Schedule
    # retire + queue next on main-thread idle.
    eos_probe_id = None

    def _on_concat_sink_event(_pad, info):
        ev = info.get_event()
        if ev and ev.type == Gst.EventType.EOS:
            is_visible = (visible_stream[0] == stream_id)
            print(f"[xfade] {stream_id} concat EOS bin {serial} "
                  f"({asset_name}) "
                  f"is_visible={is_visible}",
                  file=sys.stderr)
            # Pop queue front so sub_bin_queue reflects concat's
            # actual active (post-switch). force_eos_active reads
            # queue[0]'s filesrc to advance; an orphaned-but-not-
            # popped sub-bin would point force-EOS at the wrong
            # source. Queue pop is independent of retire timing.
            s_for_pop = streams[stream_id]
            if (s_for_pop["sub_bin_queue"]
                    and s_for_pop["sub_bin_queue"][0]["sub"]
                        is sub):
                s_for_pop["sub_bin_queue"].pop(0)
            else:
                s_for_pop["sub_bin_queue"] = [
                    e for e in s_for_pop["sub_bin_queue"]
                    if e["sub"] is not sub
                ]
                print(f"[xfade] {stream_id} EOS bin {serial} "
                      "WARN sub_bin not at queue front; "
                      "filtered.", file=sys.stderr)
            # NOTE: schedule_add is NO LONGER called from EOS per
            # QA 1c737bc soak diagnosis. The wrap hitch (60-337ms
            # gaps, 37/104 LINGERING seconds) was the new sub-bin
            # not being decode-ready when concat switched. Moving
            # schedule_add to the EAGER first-buffer probe below
            # (fires when a sub-bin becomes active) lets the next
            # sub-bin add at activation-time + 50ms, so it has the
            # full current-clip-duration to preroll AHEAD of the
            # next wrap. The blocked-pending sub-bin then has data
            # buffered at concat's sink, ready to flow the instant
            # concat unblocks it.
            # Per QA c41cf01 soak diagnosis: retire UNCONDITIONALLY
            # on idle, even when the stream is visible. The original
            # a84cee9 visible-defer was over-protective: by the
            # time GLib.idle_add fires retire, concat has already
            # absorbed the EOS and switched to the next pending
            # sub-bin. So `sub` is a SWITCHED-AWAY sub-bin at
            # retire time -- it no longer feeds the mix pad, so
            # set_state(NULL) + release_request_pad cannot stall
            # the active concat -> dec -> mix path. cutloop ships
            # exactly this pattern (always-retire-on-idle, one
            # stream, no visible/hidden distinction) with live ~2
            # and no leak. Without this, visible-side retires
            # accumulated in pending_retires across the 20s linger
            # (clip loops 4-5x), live climbed to 7, CMA exhausted
            # to ~3MB free -- near-brick (QA c41cf01 116s soak).
            GLib.idle_add(retire_subgraph, stream_id, sub,
                          concat_sink, qtdemux,
                          pad_added_id, eos_probe_id)
        return Gst.PadProbeReturn.OK
    eos_probe_id = concat_sink.add_probe(
        Gst.PadProbeType.EVENT_DOWNSTREAM, _on_concat_sink_event
    )

    # EAGER re-add per QA 1c737bc soak: install a BUFFER probe on
    # concat_sink that fires ONCE when the FIRST buffer flows
    # through (i.e. this sub-bin just became concat's active
    # source). At that moment, schedule the NEXT add so the
    # following sub-bin pre-rolls during this clip's lifetime
    # (~5-9s of preroll vs the prior pattern's ~50-250ms post-EOS).
    # By the time concat switches AGAIN at this sub-bin's natural
    # EOS, the next sub-bin has been blocked-and-buffered at
    # concat's pending pad for the whole clip duration; its data
    # flows the instant concat unblocks it. Cuts the wrap latency
    # to the dec restart on the new IDR (single-digit-ms on
    # bcm2835 for same SPS/PPS) + tiny concat-switch overhead.
    first_buffer_seen = [False]

    def _on_concat_sink_first_buffer(_pad, _info):
        if first_buffer_seen[0]:
            return Gst.PadProbeReturn.OK
        first_buffer_seen[0] = True
        print(f"[xfade] {stream_id} concat ACTIVE bin {serial} "
              f"({asset_name}) -- eager schedule_add",
              file=sys.stderr)
        # Defer to main loop so the probe returns quickly and the
        # streaming thread isn't blocked on GLib state mutation.
        GLib.idle_add(schedule_add, stream_id)
        return Gst.PadProbeReturn.REMOVE
    concat_sink.add_probe(
        Gst.PadProbeType.BUFFER, _on_concat_sink_first_buffer
    )

    _wt_probes = time.monotonic_ns()
    # Per QA 22382ad dispatch: move sub.sync_state_with_parent
    # (prime suspect for the 100-330ms main-thread block) off main
    # to the setup-worker. The worker calls set_state(PLAYING) +
    # get_state(2s); qtdemux's moov-parse runs on its task thread
    # which the worker drives. By the time concat needs this sub-
    # bin's data (at the next wrap), the worker has long since
    # completed the transition.
    _setup_q.put((sub, f"src_{stream_id}_{serial}"))
    _wt_setup_enqueue = time.monotonic_ns()
    s["live_subgraph_count"] += 1
    # Append to per-stream queue (concat-input order). Front of
    # this queue is the currently-active source feeding concat;
    # rest are pending. force_eos_active reads queue[0]'s filesrc
    # to advance on demand.
    s["sub_bin_queue"].append({"sub": sub, "filesrc": filesrc})
    print(f"[xfade] queued {stream_id} bin {serial} = "
          f"{asset_name} (live={s['live_subgraph_count']} "
          f"queue_len={len(s['sub_bin_queue'])} "
          f"added={s['playlist_added_count']})",
          file=sys.stderr)
    # Per-op timing summary for QA wrap-stall localization.
    print(
        f"[wrap-timing] {stream_id}/{serial} "
        f"create_ms={(_wt_create - _wt_start)/1e6:.0f} "
        f"link_ms={(_wt_link - _wt_create)/1e6:.0f} "
        f"ghost_ms={(_wt_ghost - _wt_link)/1e6:.0f} "
        f"pipeadd_ms={(_wt_pipeadd - _wt_ghost)/1e6:.0f} "
        f"reqpad_ms={(_wt_reqpad - _wt_pipeadd)/1e6:.0f} "
        f"padlink_ms={(_wt_padlink - _wt_reqpad)/1e6:.0f} "
        f"probes_ms={(_wt_probes - _wt_padlink)/1e6:.0f} "
        f"setup_enqueue_ms="
        f"{(_wt_setup_enqueue - _wt_probes)/1e6:.0f} "
        f"TOTAL_main_ms="
        f"{(_wt_setup_enqueue - _wt_start)/1e6:.0f}",
        file=sys.stderr
    )
    return False  # one-shot when called via GLib.idle_add


# Per QA 22382ad [stall] result: off-thread retire did NOT help
# ([stall] 109 vs 104 overruns; main thread STILL blocks 136-333ms
# at every wrap). set_state(NULL) was NOT the blocker. Suspect
# shifts to add_next_clip's sub.sync_state_with_parent() which
# transitions the new sub-bin to PLAYING and includes a
# READY->PAUSED transition where qtdemux opens the file and parses
# moov synchronously on the main thread.
#
# This commit:
#   (1) INSTRUMENTS every op in add_next_clip with per-op time
#       deltas -> [wrap-timing] log lines. The data will pin
#       which op eats the 100-330ms.
#   (2) MOVES sub.sync_state_with_parent() off the main thread to
#       a new setup-worker (analogous to retire-worker).
#
# The original 339213c diagnosis (set_state(NULL) suspected but
# wrong) is left below for history. Keep the retire-worker (now
# correctly hygienic + not harmful).
# cutloop is immune because kmssink presents from streaming
# thread / KMS pageflip, not GLib main.
#
# FIX: move the BLOCKING TRIO -- set_state(NULL) + pipeline.remove
# + concat.release_request_pad -- to a single dedicated worker
# thread. Serialize via a queue.Queue so A+B retires within ~50ms
# don't stack (each is sequential). Keep the cheap main-thread
# parts (disconnect handlers, remove probes, filter sub_bin_queue)
# on the main thread.
_retire_q = _queue_module.Queue()


def _retire_worker():
    """Single serialized worker for the blocking part of retire.
    GStreamer's set_state, bin.remove, and release_request_pad
    are thread-safe (internally mutex-protected). The worker
    drains _retire_q sequentially so A+B retires don't stack
    concurrent NULL transitions in the kernel.

    Outer try/except wraps the whole loop body per sacred-review
    MOD-1: if get(), unpack, or idle_add raises, log+continue so
    the worker survives. If the worker silently died, retires
    would accumulate in _retire_q forever (live_subgraph_count
    never decrements, CMA exhausts)."""
    while True:
        try:
            item = _retire_q.get()
            if item is None:
                break
            sub_bin, concat_sink_pad, concat, on_done, label = item
            try:
                # Strict order per QA dispatch: NULL -> remove ->
                # release.
                sub_bin.set_state(Gst.State.NULL)
                # Block until NULL is fully reached; bounded by 2s.
                sub_bin.get_state(2 * Gst.SECOND)
                pipeline.remove(sub_bin)
                concat.release_request_pad(concat_sink_pad)
            except Exception as exc:
                print(f"[xfade] retire-worker {label} WARN: "
                      f"{exc}", file=sys.stderr)
            # Notify main thread via idle_add for live-count
            # decrement + leak check.
            if on_done is not None:
                GLib.idle_add(on_done)
            _retire_q.task_done()
        except Exception as exc:
            print(f"[xfade] retire-worker LOOP WARN: {exc} "
                  "(continuing)", file=sys.stderr)


_retire_thread = threading.Thread(
    target=_retire_worker, name="xfade-retire", daemon=True
)
_retire_thread.start()


# Setup-worker per QA 22382ad [stall]-still-fires diagnosis. Moves
# sub.set_state(PLAYING) -- the main-thread-blocking call inside
# add_next_clip -- to a dedicated worker thread. Pipeline.add and
# pad linking still happen on main (they are cheap; the suspect is
# the state-change cascade that triggers qtdemux's moov parse).
_setup_q = _queue_module.Queue()


def _setup_worker():
    """Single serialized worker for sub-bin state-change-to-PLAYING.
    Frees the GLib main thread from the qtdemux moov-parse +
    state-change cascade that was blocking it 100-330ms at every
    wrap. Outer try/except per sacred-review MOD-1."""
    while True:
        try:
            item = _setup_q.get()
            if item is None:
                break
            sub_bin, label = item
            try:
                t0 = time.monotonic_ns()
                ret = sub_bin.set_state(Gst.State.PLAYING)
                t1 = time.monotonic_ns()
                # Bound the worker's get_state wait so a stuck
                # transition does not block subsequent setups.
                state_ret, _cur, _pen = sub_bin.get_state(
                    2 * Gst.SECOND
                )
                t2 = time.monotonic_ns()
                print(f"[setup-worker] {label} "
                      f"set_state_ms={(t1-t0)/1e6:.0f} "
                      f"get_state_ms={(t2-t1)/1e6:.0f} "
                      f"ret={ret.value_nick} "
                      f"state_ret={state_ret.value_nick}",
                      file=sys.stderr)
            except Exception as exc:
                print(f"[setup-worker] {label} WARN: {exc}",
                      file=sys.stderr)
            _setup_q.task_done()
        except Exception as exc:
            print(f"[setup-worker] LOOP WARN: {exc} (continuing)",
                  file=sys.stderr)


_setup_thread = threading.Thread(
    target=_setup_worker, name="xfade-setup", daemon=True
)
_setup_thread.start()


def retire_subgraph(stream_id, sub_bin, concat_sink_pad,
                    qtdemux_elem, pad_added_id, eos_probe_id):
    """Split per QA 339213c [stall]-data fix:
    MAIN THREAD (this fn):
      - Disconnect qtdemux pad-added handler (closure-leak).
      - Remove concat-sink EOS probe (closure-leak).
      - Filter sub_bin_queue defensively.
    WORKER THREAD (_retire_worker, via _retire_q):
      - sub_bin.set_state(Gst.State.NULL) (the blocker, ~150-270ms).
      - pipeline.remove(sub_bin).
      - concat.release_request_pad(concat_sink_pad).
    POST (via GLib.idle_add from worker):
      - Decrement live_subgraph_count.
      - Run weakref leak-check.
      - Emit retire-complete log line.

    Holding sub_bin/concat_sink_pad refs in the queue item keeps
    them alive across the worker's set_state(NULL) (no GC mid-
    teardown). cutloop's leak-safe disconnect-before-NULL
    discipline is preserved: handlers/probes are released on the
    main thread BEFORE the worker touches the elements."""
    s = streams[stream_id]
    name = sub_bin.get_name() if sub_bin else "?"
    weak = weakref.ref(sub_bin)
    try:
        # (1) Main-thread-safe closures release.
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
        # (2) Filter sub_bin_queue defensively (the EOS probe
        # has already popped on normal paths).
        before_len = len(s["sub_bin_queue"])
        s["sub_bin_queue"] = [
            e for e in s["sub_bin_queue"]
            if e["sub"] is not sub_bin
        ]
        if len(s["sub_bin_queue"]) != before_len:
            print(f"[xfade] retire {name} WARN sub_bin was "
                  "still in queue; filtered.", file=sys.stderr)

        # (3) Enqueue the BLOCKING TRIO for the worker thread.
        def _on_retire_complete():
            s["live_subgraph_count"] -= 1
            print(f"[xfade] retire {name} complete "
                  f"(live_{stream_id}="
                  f"{s['live_subgraph_count']} "
                  f"queue_len={len(s['sub_bin_queue'])})",
                  file=sys.stderr)
            # Leak-check now (closures + queue ref dropped;
            # weakref should resolve to None).
            def _check_leak():
                gc.collect()
                if weak() is not None:
                    print(f"[xfade] [retire] LEAK ref still "
                          f"held for {name}", file=sys.stderr)
                return False
            GLib.idle_add(_check_leak)
            return False  # one-shot idle_add
        _retire_q.put(
            (sub_bin, concat_sink_pad, s["concat"],
             _on_retire_complete, name)
        )
    except Exception as exc:
        print(f"[xfade] retire {name} WARN: {exc}",
              file=sys.stderr)

    # NOTE: schedule_add (queue the next sub-bin) is independent
    # of retire. Under the EAGER pattern (post-1c737bc) it fires
    # from the FIRST-BUFFER probe on concat_sink, not from the
    # EOS probe. The retire-worker handles set_state(NULL) +
    # remove + release_request_pad off-main-thread; the main
    # thread returns immediately, freeing the GLib main loop to
    # service the GL output without the 150-270ms stall.
    return False  # one-shot


# Pre-queue INITIAL_QUEUE_DEPTH clips per stream BEFORE preroll.
for sid in ("A", "B"):
    for _ in range(INITIAL_QUEUE_DEPTH):
        add_next_clip(sid)


# --- Crossfade orchestrator (clip-gated linger) ---------------------
#
# Per QA glass feedback (qarl): the v1 fixed 4s TARGET_VISIBLE_S
# fires mid-clip and produces weird drift/cutting. Replace with a
# clip-gated linger:
#   - Each visible clip plays FULL LENGTH AND >= LINGER_MIN_S
#     (short clips LOOP via re-queue of the same clip).
#   - Crossfade ONLY at a CLEAN boundary: the incoming stream
#     is FORCE-ADVANCED to its next clip so its first frame is
#     FROM ITS START at the crossfade moment.
#
# Phases:
#   LINGERING_VISIBLE: visible stream is showing current clip.
#       Polled every LINGER_CHECK_MS. When elapsed wall-clock
#       since visible_turn_started >= linger_target, transition
#       to ADVANCING_INCOMING.
#   ADVANCING_INCOMING: incoming stream's current_loop_clip is
#       reassigned to its next playlist entry; 2x force_eos_active
#       so concat advances pending -> new clip from frame 0. Then
#       wait ADVANCE_FIRST_FRAME_WAIT_MS for the new clip's
#       buffers to flow through dec/queue/glupload/mix.
#   FADING: linear alpha animation over FADE_S. On completion:
#       swap visible_stream, stamp new visible_turn_started, set
#       new linger_target, re-enter LINGERING_VISIBLE.

visible_stream = ["A"]
linger_state = {
    "phase": "LINGERING_VISIBLE",
    "visible_turn_started_ns": 0,
    "visible_turn_clip": None,
    "linger_target_s": 0.0,
    "advance_eos_count": 0,
    "fade_started_ns": 0,
    "from_stream": None,
    "to_stream": None,
}


def _enter_lingering_visible():
    """Stamp the visible-turn state and arm the LINGER_CHECK
    poll. Called at startup (initial visible turn) and after
    each crossfade completes."""
    vis = visible_stream[0]
    s = streams[vis]
    clip = s["current_loop_clip"]
    dur = CLIP_DURATIONS.get(clip, DUR_FALLBACK_S)
    target = max(dur, LINGER_MIN_S)
    linger_state["phase"] = "LINGERING_VISIBLE"
    linger_state["visible_turn_started_ns"] = (
        GLib.get_monotonic_time() * 1000
    )
    linger_state["visible_turn_clip"] = clip
    linger_state["linger_target_s"] = target
    name = os.path.basename(os.path.dirname(clip)) or clip
    print(f"[xfade] LINGER start: visible={vis} clip={name} "
          f"dur={dur:.2f}s linger_target={target:.2f}s",
          file=sys.stderr)


def _linger_check():
    """1Hz-ish poll: if visible turn has elapsed past the linger
    target, transition to ADVANCING_INCOMING. Returns True to
    re-arm the timeout."""
    if linger_state["phase"] != "LINGERING_VISIBLE":
        return True
    now_ns = GLib.get_monotonic_time() * 1000
    elapsed_s = (
        (now_ns - linger_state["visible_turn_started_ns"]) / 1e9
    )
    if elapsed_s >= linger_state["linger_target_s"]:
        _start_advancing()
    return True


def force_eos_active(stream_id):
    """Send EOS to the currently-active source bin's filesrc
    (front of sub_bin_queue). concat then switches to the next
    queued sub-bin (pending) and the existing EOS-probe path
    retires the just-EOSed sub-bin + queues another."""
    s = streams[stream_id]
    if not s["sub_bin_queue"]:
        print(f"[xfade] force_eos_active {stream_id} WARN: "
              "sub_bin_queue empty; skipping",
              file=sys.stderr)
        return
    front = s["sub_bin_queue"][0]
    filesrc = front.get("filesrc")
    if filesrc is None:
        print(f"[xfade] force_eos_active {stream_id} WARN: "
              "front filesrc is None; skipping",
              file=sys.stderr)
        return
    print(f"[xfade] force_eos_active {stream_id} (queue_len="
          f"{len(s['sub_bin_queue'])})", file=sys.stderr)
    filesrc.send_event(Gst.Event.new_eos())


def _force_eos_with_pre_fortify(stream_id):
    """force_eos_active wrapped with a pre-fortify guarantee that
    the queue has at least one pending sub-bin BEFORE the force-
    EOS pops the active. Crash fix per QA 36ed49b soak: the
    EAGER schedule_add timing (fires from the first-buffer probe
    when concat unblocks the new active, plus ADD_AFTER_RETIRE_MS
    + cross-stream MIN_INTER_ADD_MS serialization) can leave the
    incoming stream's queue at exactly 1 (active only, no
    pending) at a crossfade moment. force_eos_active would then
    pop the active -> queue=0 -> concat has no pending -> emits
    downstream EOS -> pipeline EOS -> shutdown.

    SYNCHRONOUS add_next_clip is used here (bypassing the
    MIN_INTER_ADD_MS gate). Acceptable because this fires at
    crossfade time, a controlled moment, not steady state; CMA
    peak overshoot is ~1-2MB per added sub-bin (filesrc +
    qtdemux + h264parse only, no decoder allocation), well
    within the 1c737bc->36ed49b CMA headroom (~51-53MB CmaFree
    projection)."""
    s = streams[stream_id]
    while len(s["sub_bin_queue"]) < 2:
        print(f"[xfade] pre-fortify {stream_id} queue_len="
              f"{len(s['sub_bin_queue'])} < 2; synchronous "
              "add_next_clip to keep concat fed past force-EOS",
              file=sys.stderr)
        add_next_clip(stream_id)
    # Per setup-worker review: pre-fortify must guarantee data-
    # ready, not just queue-len >= 2. add_next_clip now enqueues
    # state-change to PLAYING on the setup worker; the new bin is
    # in NULL/READY until the worker transitions it. Drain the
    # setup queue here so force-EOS can switch concat to a
    # PLAYING pending pad with imminent buffers, not a not-yet-
    # transitioned pad. Fires at most twice per crossfade
    # (1st + 2nd force-EOS) and blocks main only as long as the
    # worker would have blocked main under the pre-22382ad code.
    # Preserves the 36ed49b drain-to-zero crash protection.
    _setup_q.join()
    force_eos_active(stream_id)


def _start_advancing():
    """LINGERING_VISIBLE -> ADVANCING_INCOMING.
    On the incoming's FIRST visible turn: KEEP current_loop_clip
    (the stream was initialized with the correct first clip);
    force-EOS still fires for "fresh from frame 0" semantics.
    On subsequent crossfades-to-this-stream: reassign
    incoming.current_loop_clip to its next playlist entry.
    Either way, schedule 2x force_eos_active so concat advances
    past the still-pending OLD loop clip to the freshly-queued
    new (or same) clip. The 2nd force-EOS is deferred to allow
    the 1st cycle's retire+add (ADD_AFTER_RETIRE_MS=50ms) to
    complete."""
    from_id = visible_stream[0]
    to_id = "B" if from_id == "A" else "A"
    incoming = streams[to_id]
    if incoming["has_been_visible"]:
        new_idx = incoming["next_idx"] % len(PLAYLIST)
        new_clip = PLAYLIST[new_idx]
        incoming["next_idx"] += 2  # alternating playlist stride
        old_clip = incoming["current_loop_clip"]
        incoming["current_loop_clip"] = new_clip
        new_name = (os.path.basename(os.path.dirname(new_clip))
                    or new_clip)
        old_name = (os.path.basename(os.path.dirname(old_clip))
                    or old_clip)
        print(f"[xfade] ADVANCE incoming={to_id}: "
              f"loop_clip {old_name} -> {new_name} "
              f"(playlist idx={new_idx})", file=sys.stderr)
    else:
        # First crossfade-to-this-stream. KEEP current_loop_clip
        # (it was initialized correctly at startup). Force-EOS
        # still fires below so the incoming is shown from
        # frame 0 at the crossfade moment. Per reviewer catch
        # of v2-pre-bugfix where B would show clip0 (same as
        # A's prior turn) instead of its initialized clip1.
        incoming["has_been_visible"] = True
        keep_name = (os.path.basename(
            os.path.dirname(incoming["current_loop_clip"])
        ) or incoming["current_loop_clip"])
        print(f"[xfade] ADVANCE incoming={to_id}: "
              f"FIRST visible turn -- KEEP loop_clip={keep_name} "
              "(force-EOS will reset to frame 0)",
              file=sys.stderr)
    linger_state["phase"] = "ADVANCING_INCOMING"
    linger_state["advance_eos_count"] = 0
    linger_state["from_stream"] = from_id
    linger_state["to_stream"] = to_id
    # 1st force-EOS: concat advances active (old_loop) -> pending
    # (old_loop again). The retire+add cycle then queues NEW_clip
    # as the new pending. After ADD_AFTER_RETIRE_MS the new clip
    # is the pending of the now-active old_loop.
    # _force_eos_with_pre_fortify guarantees queue >= 2 (active +
    # >=1 pending) BEFORE issuing force-EOS -- prevents the
    # 36ed49b drain-to-zero crash when EAGER hasn't replenished.
    _force_eos_with_pre_fortify(to_id)
    linger_state["advance_eos_count"] += 1
    # 2nd force-EOS: scheduled to fire AFTER the first retire+add
    # cycle so the pending is now new_clip; this EOS switches
    # concat to it, making new_clip active from frame 0.
    # ADD_AFTER_RETIRE_MS = 50ms covers the retire idle + the
    # deferred add timer; pad another 100ms for the new sub-bin
    # to sync_state_with_parent and become a real pending in
    # concat's queue.
    GLib.timeout_add(ADD_AFTER_RETIRE_MS + 100, _second_advance_eos)


def _second_advance_eos():
    """Fire the 2nd force-EOS so the now-pending new clip
    becomes active. Then schedule the wait-for-first-frame
    before starting the alpha animation."""
    if linger_state["phase"] != "ADVANCING_INCOMING":
        return False
    to_id = linger_state["to_stream"]
    # Same pre-fortify guard as the 1st force-EOS. Between the
    # 1st and 2nd, EAGER's first-buffer probe on the new active
    # may not have fired+materialized an add yet (~50-300ms vs
    # this timer's 150ms). Without the guard, the 2nd force-EOS
    # could be the one to drain queue to zero.
    _force_eos_with_pre_fortify(to_id)
    linger_state["advance_eos_count"] += 1
    # Wait ADVANCE_FIRST_FRAME_WAIT_MS for the new clip's
    # buffers to flow through dec -> queue -> glupload -> mix
    # before starting the crossfade. bcm2835 first-frame
    # latency drives this; tuned for ~500-1000ms.
    GLib.timeout_add(ADVANCE_FIRST_FRAME_WAIT_MS, _start_fade)
    return False


def _start_fade():
    """ADVANCING_INCOMING -> FADING. Stamp the fade start and
    arm the alpha-animation tick. The incoming stream's
    current active is now the new_clip from ~frame 0."""
    if linger_state["phase"] != "ADVANCING_INCOMING":
        return False
    linger_state["phase"] = "FADING"
    linger_state["fade_started_ns"] = GLib.get_monotonic_time() * 1000
    from_id = linger_state["from_stream"]
    to_id = linger_state["to_stream"]
    print(f"[xfade] FADE start: {from_id} -> {to_id}",
          file=sys.stderr)
    GLib.timeout_add(FADE_TICK_MS, _fade_tick)
    return False


def _fade_tick():
    """Per-tick linear alpha animation. On completion: swap
    visible_stream, re-enter LINGERING_VISIBLE."""
    if linger_state["phase"] != "FADING":
        return False
    from_id = linger_state["from_stream"]
    to_id = linger_state["to_stream"]
    from_pad = streams[from_id]["mix_pad"]
    to_pad = streams[to_id]["mix_pad"]
    now_ns = GLib.get_monotonic_time() * 1000
    elapsed_s = (now_ns - linger_state["fade_started_ns"]) / 1e9
    if elapsed_s >= FADE_S:
        from_pad.set_property("alpha", 0.0)
        to_pad.set_property("alpha", 1.0)
        visible_stream[0] = to_id
        print(f"[xfade] FADE complete; visible={to_id}",
              file=sys.stderr)
        # Both streams use the same concat-re-add gapless loop
        # AND the same unconditional-retire-on-idle pattern
        # (cutloop's proven model). No deferred-retire drain
        # is needed at fade-completion -- retires ran inline
        # via idle as each sub-bin EOSed.
        _enter_lingering_visible()
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


def _make_gap_probe(state):
    """BUFFER probe that records frames + max inter-buffer gap (ms)
    per fps_tick interval. State is a {"frames", "last_ns",
    "max_gap_ms"} dict that fps_tick reads + resets each second."""
    def _on_buf(_pad, _info):
        now = time.monotonic_ns()
        state["frames"] += 1
        last = state["last_ns"]
        if last:
            gap_ms = (now - last) / 1e6
            if gap_ms > state["max_gap_ms"]:
                state["max_gap_ms"] = gap_ms
        state["last_ns"] = now
        return Gst.PadProbeReturn.OK
    return _on_buf


sink_pad_for_probe.add_probe(
    Gst.PadProbeType.BUFFER, _make_gap_probe(screen_state)
)


# Main-loop-stall detector per QA 12458ae dispatch. The 3 heavy
# per-buffer probes from 446511d CONTAMINATED the measurement:
# 4 pads x 2 streams x 24fps = ~192 Python-callback fires/sec
# adding GIL load that's itself burstier at retire/re-add moments,
# making the instrumented soak look worse (6%) than uninstrumented
# 8e2215e (66%). Replace with a single low-frequency timer that
# logs ONLY when its actual wall-clock interval overruns a
# threshold = the GLib main thread was blocked that long. Near-
# zero overhead (one timer, logs only on overrun). If overruns
# correlate with the wrap-hitch seconds, the retire/re-add path
# IS blocking the main thread and a non-blocking refactor is the
# next fix. If main loop is smooth but screen still hitches, the
# hitch is in the GL/decode path (different fix).
STALL_CHECK_INTERVAL_MS = 20
STALL_THRESHOLD_MS = int(os.environ.get(
    "OPENMARQUEE_XFADE_STALL_THRESHOLD_MS", "50"
))
stall_state = {"last_ns": 0, "overruns_this_sec": 0,
                "max_overrun_ms": 0}


def _stall_check():
    now = time.monotonic_ns()
    last = stall_state["last_ns"]
    if last:
        interval_ms = (now - last) / 1e6
        if interval_ms > STALL_THRESHOLD_MS:
            stall_state["overruns_this_sec"] += 1
            if interval_ms > stall_state["max_overrun_ms"]:
                stall_state["max_overrun_ms"] = int(interval_ms)
            uptime = int(time.monotonic() - _t_start)
            print(f"[stall] t={uptime} interval={int(interval_ms)}ms "
                  f"over threshold={STALL_THRESHOLD_MS}ms "
                  f"(main GLib thread was blocked)",
                  file=sys.stderr, flush=True)
    stall_state["last_ns"] = now
    return True


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
    # Linger orchestrator visibility: which phase + remaining
    # wall-clock on the current visible turn (if applicable).
    phase = linger_state["phase"]
    if phase == "LINGERING_VISIBLE":
        now_ns = GLib.get_monotonic_time() * 1000
        elapsed_s = (
            (now_ns - linger_state["visible_turn_started_ns"]) / 1e9
        )
        rem = linger_state["linger_target_s"] - elapsed_s
        linger_rem_str = f"linger_rem={max(0.0, rem):.1f}s"
    else:
        linger_rem_str = "linger_rem=-"
    line = (
        f"[fps] t={uptime} "
        f"screen={screen_state['frames']} "
        f"max_gap_ms={int(screen_state['max_gap_ms'])} "
        f"visible={vis} "
        f"alphaA={a_alpha:.2f} alphaB={b_alpha:.2f} "
        f"phase={phase} {linger_rem_str} "
        f"live_A={streams['A']['live_subgraph_count']} "
        f"live_B={streams['B']['live_subgraph_count']} "
        f"added_A={streams['A']['playlist_added_count']} "
        f"added_B={streams['B']['playlist_added_count']} "
        f"CmaFree_kB={cma_kb} "
        f"CmaFree_min_kB={cma_min_str}"
    )
    print(line, file=sys.stderr, flush=True)
    # Main-loop-stall summary (per QA 12458ae dispatch). Counts +
    # max overrun observed this 1s window. Per-overrun lines are
    # already emitted by _stall_check at the moment they happen;
    # this summary makes the data trivial to correlate with [fps].
    print(
        f"[stall-1s] t={uptime} "
        f"overruns={stall_state['overruns_this_sec']} "
        f"max_overrun_ms={stall_state['max_overrun_ms']}",
        file=sys.stderr, flush=True
    )
    stall_state["overruns_this_sec"] = 0
    stall_state["max_overrun_ms"] = 0
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

# Enter the initial LINGERING_VISIBLE state for the startup
# visible turn (A is visible by alpha=1.0/0.0). The visible
# stream loops its current_loop_clip via concat-re-add: each
# natural EOS triggers schedule_add (the EOS probe in
# add_next_clip is UNCONDITIONAL on visibility), which queues
# another sub-bin of the SAME clip; concat advances gaplessly.
# No seek, no flush, no HW-decoder repositioning. Arm the
# linger poll at LINGER_CHECK_MS.
_enter_lingering_visible()
GLib.timeout_add(LINGER_CHECK_MS, _linger_check)
GLib.timeout_add(1000, fps_tick)
# Main-loop-stall detector at ~20ms. Logs only when its actual
# wall-clock interval exceeds STALL_THRESHOLD_MS (default 50ms)
# = the GLib main thread was blocked that long. Cheap (~50 fires/
# sec doing arithmetic + a comparison). No buffer probes, no
# per-frame Python callback. Per QA 12458ae dispatch.
GLib.timeout_add(STALL_CHECK_INTERVAL_MS, _stall_check)


try:
    loop.run()
finally:
    print("[xfade] shutdown -> NULL", file=sys.stderr)
    pipeline.set_state(Gst.State.NULL)
    print("[xfade] done", file=sys.stderr)
