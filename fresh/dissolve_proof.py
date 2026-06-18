#!/usr/bin/env python3
"""fresh/dissolve_proof.py -- one-shot A->B crossfade PNG capture.

Per QA dispatch 2026-06-18 (after qarl's "real PIXELS not
counters" challenge): build a standalone proof that captures
PNG frames across a single A->B GL crossfade, so we can SEE
whether the GPU blend actually produces a dissolve.

NOT the renderer. NOT a probe of shared-context or cycling.
ONE pipeline, ONE A->B fade, capture only. Deliberately
sidesteps every wall the iterative cutfade work has hit
(re-prime aggregator starve, idle-pad wait, context share,
display starve) and isolates the single question: does
glvideomixer visually blend two video frames?

PIPELINE:
  filesrc(A) ! qtdemux ! h264parse ! v4l2h264dec ! glupload --.
                                                              v
  filesrc(B) ! qtdemux ! h264parse ! v4l2h264dec ! glupload --.
                                                              v
                            glvideomixer name=mix sink_0+sink_1
                                                              |
                                                              v
                gldownload ! videoconvert ! pngenc ! multifilesink
                              location=/tmp/dissolve_proof_%02d.png

CAPTURE STRATEGY:
  A BUFFER probe on mix.src DROPs every buffer by default. After
  letting both decoders warm up (1.5s after PLAYING), step through
  5 alpha pairs at ~150ms apart. At each step set the new alpha,
  flip a "want_capture" flag; the very next buffer through mix.src
  passes the probe (counted as captured for this step), gets
  written by multifilesink to the auto-indexed PNG, and the
  probe goes back to DROPping. So multifilesink sees exactly 5
  buffers and writes /tmp/dissolve_proof_00.png .. _04.png.

ALPHA SCHEDULE -> PNG:
  00: alpha_A=1.00 alpha_B=0.00 -> pure A
  01: alpha_A=0.75 alpha_B=0.25 -> 75/25 blend
  02: alpha_A=0.50 alpha_B=0.50 -> 50/50 blend (THE money frame)
  03: alpha_A=0.25 alpha_B=0.75 -> 25/75 blend
  04: alpha_A=0.00 alpha_B=1.00 -> pure B

SUCCESS CRITERIA on pixels:
  - 00 looks like pure A clip frame.
  - 04 looks like pure B clip frame.
  - 02 shows BOTH visibly mixed (ghosted together).
  - 01 / 03 show partial blends.
  - If 02 is pure A or pure B, the blend is broken.
  - If all 5 PNGs are black or garbage, the GL chain itself
    is broken (no blend test possible).

DO NOT touch cutloop.py or cutfade.py. cutloop stays sign
default; QA stops it briefly, runs the proof, restores cutloop.
"""

import os
import sys

# GL env MUST be set before Gst.init.
os.environ.setdefault("GST_GL_PLATFORM", "egl")
os.environ.setdefault("GST_GL_WINDOW", "gbm")
os.environ.setdefault("GST_GL_API", "gles2")


def _ensure_xdg_runtime_dir():
    """systemd transient units launched without logind get no
    XDG_RUNTIME_DIR. Mesa vc4 EGL/GBM uses it; cold-compile
    blows past 10s without it. Mirrors cutfade/cutloop."""
    if os.environ.get("XDG_RUNTIME_DIR"):
        return
    uid = os.getuid()
    for candidate in (f"/run/user/{uid}", f"/tmp/runtime-{uid}"):
        try:
            os.makedirs(candidate, mode=0o700, exist_ok=True)
            os.chmod(candidate, 0o700)
            os.environ["XDG_RUNTIME_DIR"] = candidate
            print(f"[proof] XDG_RUNTIME_DIR fallback: {candidate}",
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
print("[proof] Gst initialized", file=sys.stderr)


# --- Config -----------------------------------------------------------

CLIP_A = ("/var/openmarquee/content/"
          "0ef82ed1-3699-4cd8-9c70-bdb8d4752e1e/asset.mp4")
CLIP_B = ("/var/openmarquee/content/"
          "779d99e5-9742-4412-9025-f60187477bd2/asset.mp4")
OUTPUT_PATTERN = "/tmp/dissolve_proof_%02d.png"

# Alpha schedule per QA dispatch: (alpha_A, alpha_B) per step.
ALPHA_STEPS = [
    (1.00, 0.00),  # 00 pure A
    (0.75, 0.25),  # 01
    (0.50, 0.50),  # 02 the money frame
    (0.25, 0.75),  # 03
    (0.00, 1.00),  # 04 pure B
]

WARMUP_MS = 1500       # decoder warmup before captures begin
STEP_INTERVAL_MS = 200 # between successive alpha+capture steps
FINISH_DELAY_MS = 800  # after last capture, flush pngenc+sink
# Absolute timeout watchdog. With re-arm enabled, the script
# could in principle wait indefinitely if a decoder wedges.
# This fires regardless and forces _finish so QA's transient
# unit always exits in bounded time. 30s is generous against
# the longest plausible v4l2h264dec cold-start tail observed
# on this Pi.
WATCHDOG_TIMEOUT_MS = 30000

PREROLL_BUDGET_S = 30


def die(msg, code=1):
    print(f"[proof] FATAL {msg}", file=sys.stderr)
    sys.exit(code)


# Verify clips + clear any stale prior PNGs.
for path in (CLIP_A, CLIP_B):
    if not os.path.isfile(path):
        die(f"clip not found: {path}")
for i in range(len(ALPHA_STEPS)):
    p = OUTPUT_PATTERN % i
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass
print(f"[proof] CLIP_A={CLIP_A}", file=sys.stderr)
print(f"[proof] CLIP_B={CLIP_B}", file=sys.stderr)
print(f"[proof] OUTPUT_PATTERN={OUTPUT_PATTERN} "
      "(any stale prior PNGs cleared)", file=sys.stderr)


# --- Pipeline ---------------------------------------------------------

pipeline = Gst.Pipeline.new("dissolve_proof")
mix = Gst.ElementFactory.make("glvideomixer", "mix")
if mix is None:
    die("glvideomixer factory returned None")
pipeline.add(mix)

mix_pad_a = mix.request_pad_simple("sink_%u")
mix_pad_b = mix.request_pad_simple("sink_%u")
if mix_pad_a is None or mix_pad_b is None:
    die("mix.request_pad_simple failed")
mix_pad_a.set_property("alpha", 1.0)
mix_pad_a.set_property("zorder", 0)
mix_pad_b.set_property("alpha", 0.0)
mix_pad_b.set_property("zorder", 1)


def _build_branch(label, location, mix_sink_pad):
    """Build filesrc -> qtdemux -> h264parse -> v4l2h264dec ->
    glupload sub-bin and link its ghost output to mix_sink_pad.
    Identical to cutfade's pattern minus the lifecycle bookkeeping."""
    sub = Gst.Bin.new(f"branch_{label}")
    filesrc = Gst.ElementFactory.make("filesrc", None)
    filesrc.set_property("location", location)
    qtdemux = Gst.ElementFactory.make("qtdemux", None)
    h264parse = Gst.ElementFactory.make("h264parse", None)
    h264parse.set_property("config-interval", -1)
    decoder = Gst.ElementFactory.make(
        "v4l2h264dec", f"dec_{label}"
    )
    glupload = Gst.ElementFactory.make("glupload", None)
    for el in (filesrc, qtdemux, h264parse, decoder, glupload):
        if el is None:
            die(f"[{label}] factory.make returned None")
        sub.add(el)
    if not filesrc.link(qtdemux):
        die(f"[{label}] filesrc -> qtdemux link failed")

    def _on_pad_added(_demux, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        caps_str = caps.to_string() if caps else ""
        if not caps_str.startswith("video/"):
            return
        sink_pad = h264parse.get_static_pad("sink")
        if sink_pad and not sink_pad.is_linked():
            res = pad.link(sink_pad)
            if res != Gst.PadLinkReturn.OK:
                print(f"[proof] [{label}] qtdemux video link "
                      f"failed: {res}", file=sys.stderr)

    qtdemux.connect("pad-added", _on_pad_added)
    if not h264parse.link(decoder):
        die(f"[{label}] h264parse -> v4l2h264dec link failed")
    if not decoder.link(glupload):
        die(f"[{label}] v4l2h264dec -> glupload link failed")
    glupload_src = glupload.get_static_pad("src")
    ghost = Gst.GhostPad.new("src", glupload_src)
    sub.add_pad(ghost)
    pipeline.add(sub)
    if ghost.link(mix_sink_pad) != Gst.PadLinkReturn.OK:
        die(f"[{label}] ghost -> mix.sink link failed")
    return sub


branch_a = _build_branch("A", CLIP_A, mix_pad_a)
branch_b = _build_branch("B", CLIP_B, mix_pad_b)
print("[proof] both source branches built", file=sys.stderr)


# Post-mixer chain: mix -> gldownload -> videoconvert -> pngenc
# -> multifilesink. gldownload is slow but this is one-shot
# capture, not real-time.
gldownload = Gst.ElementFactory.make("gldownload", None)
videoconvert = Gst.ElementFactory.make("videoconvert", None)
pngenc = Gst.ElementFactory.make("pngenc", None)
multifilesink = Gst.ElementFactory.make("multifilesink", None)
for el in (gldownload, videoconvert, pngenc, multifilesink):
    if el is None:
        die("post-mix factory.make returned None")
    pipeline.add(el)
multifilesink.set_property("location", OUTPUT_PATTERN)
multifilesink.set_property("post-messages", False)
if not mix.link(gldownload):
    die("mix -> gldownload link failed")
if not gldownload.link(videoconvert):
    die("gldownload -> videoconvert link failed")
if not videoconvert.link(pngenc):
    die("videoconvert -> pngenc link failed")
if not pngenc.link(multifilesink):
    die("pngenc -> multifilesink link failed")
print("[proof] post-mix chain built", file=sys.stderr)


# --- Capture state machine -------------------------------------------

class CaptureState:
    step = 0
    want_capture = False
    captured = False


cap = CaptureState()


def _on_mix_src(_pad, _info):
    """DROP every buffer leaving the mixer except when want_capture
    is True and we haven't already captured for this step. That one
    buffer passes through and reaches multifilesink, which writes
    the next auto-indexed PNG."""
    if not cap.want_capture or cap.captured:
        return Gst.PadProbeReturn.DROP
    cap.captured = True
    cap.want_capture = False
    return Gst.PadProbeReturn.OK


mix_src = mix.get_static_pad("src")
if mix_src is None:
    die("mix has no src pad")
mix_src.add_probe(Gst.PadProbeType.BUFFER, _on_mix_src)


def _step_capture():
    # Re-arm if the PRIOR step is still pending (the v4l2h264dec
    # on bcm2835 can take >1.5s for its first decoded buffer,
    # especially with two decoder instances competing). Without
    # this guard we'd burn through all 5 alpha steps before any
    # PNG was actually captured and end up with 5 MISSING files.
    # `want_capture==True` means we are still waiting on the
    # buffer for the previous step. Just wait another tick.
    if cap.want_capture and not cap.captured and cap.step > 0:
        print(f"[proof] step {cap.step - 1} still pending (no "
              "buffer through mix.src yet); re-arming",
              file=sys.stderr)
        return True
    if cap.step >= len(ALPHA_STEPS):
        print(f"[proof] all {len(ALPHA_STEPS)} captures requested; "
              f"waiting {FINISH_DELAY_MS}ms for pngenc + "
              "multifilesink to flush the last frame",
              file=sys.stderr)
        GLib.timeout_add(FINISH_DELAY_MS, _finish)
        return False
    alpha_a, alpha_b = ALPHA_STEPS[cap.step]
    mix_pad_a.set_property("alpha", alpha_a)
    mix_pad_b.set_property("alpha", alpha_b)
    out_path = OUTPUT_PATTERN % cap.step
    print(f"[proof] step {cap.step}: alpha_A={alpha_a:.2f} "
          f"alpha_B={alpha_b:.2f} -> {out_path}",
          file=sys.stderr)
    cap.captured = False
    cap.want_capture = True
    cap.step += 1
    return True


def _finish():
    print("[proof] finishing; quitting main loop", file=sys.stderr)
    loop.quit()
    return False


# --- Bus -------------------------------------------------------------

loop = GLib.MainLoop()


def _on_bus(_bus, msg):
    if msg.type == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        src = msg.src.get_name() if msg.src else "?"
        print(f"[proof] ERROR {src}: {err.message}",
              file=sys.stderr)
        if dbg:
            print(f"[proof]  debug: {dbg}", file=sys.stderr)
        loop.quit()
    elif msg.type == Gst.MessageType.EOS:
        print("[proof] pipeline EOS", file=sys.stderr)
        loop.quit()


bus = pipeline.get_bus()
bus.add_signal_watch()
bus.connect("message", _on_bus)


# --- Run -------------------------------------------------------------

print("[proof] PAUSED + preroll", file=sys.stderr)
if pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
    die("set_state PAUSED failed")
ret, cur, pending = pipeline.get_state(PREROLL_BUDGET_S * Gst.SECOND)
if ret == Gst.StateChangeReturn.FAILURE:
    die(f"preroll FAILURE state={cur.value_nick} "
        f"pending={pending.value_nick}")
print(f"[proof] preroll done ret={ret.value_nick} "
      f"state={cur.value_nick}", file=sys.stderr)

print("[proof] PLAYING", file=sys.stderr)
if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
    die("set_state PLAYING failed")

print(f"[proof] decoder warmup for {WARMUP_MS}ms then start "
      f"capture (step interval {STEP_INTERVAL_MS}ms)",
      file=sys.stderr)


def _start_capture_timer():
    GLib.timeout_add(STEP_INTERVAL_MS, _step_capture)
    return False


def _watchdog_finish():
    """Absolute-time finalizer. Fires regardless of step-machine
    progress so the script can never hang on a wedged decoder."""
    print(f"[proof] WATCHDOG: {WATCHDOG_TIMEOUT_MS}ms elapsed "
          "since PLAYING; forcing finish. Inventory below will "
          "show which captures (if any) made it.",
          file=sys.stderr)
    loop.quit()
    return False


GLib.timeout_add(WARMUP_MS, _start_capture_timer)
GLib.timeout_add(WATCHDOG_TIMEOUT_MS, _watchdog_finish)


try:
    loop.run()
finally:
    print("[proof] NULL", file=sys.stderr)
    pipeline.set_state(Gst.State.NULL)
    # Report what was written so QA can immediately spot
    # missing captures.
    print("[proof] final PNG inventory:", file=sys.stderr)
    for i in range(len(ALPHA_STEPS)):
        p = OUTPUT_PATTERN % i
        if os.path.isfile(p):
            sz = os.path.getsize(p)
            alpha_a, alpha_b = ALPHA_STEPS[i]
            print(f"[proof]   {p}  size={sz}B  "
                  f"alpha_A={alpha_a:.2f} alpha_B={alpha_b:.2f}",
                  file=sys.stderr)
        else:
            print(f"[proof]   {p}  MISSING", file=sys.stderr)
    print("[proof] done", file=sys.stderr)
