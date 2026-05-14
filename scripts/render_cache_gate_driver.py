#!/usr/bin/env python3
"""Cache-regression gate driver. Runs on dev Pi.

Drives the IPC sidecar for ~N frames of ONE slide, captures the
per-frame `OPENMARQUEE_BOUNDARY_TRACE` JSON from stderr, and asserts
that AFTER the first --warmup frames every painted frame's
`total_us` is below `--budget-ms`.

The bug class this gate locks in: 9e776e7 + e6f914e wired
`EglSession::slide_caches` into the IPC sidecar's paint + transition
paths. Pre-cache, the heavy FYS slides hit 100% over-budget frames
(see qa/sidecar-sustained-smoke-2026-05-13.md). With cache wired,
the over-budget rate drops to 0.24% and p99 lands at 29.1 ms (see
qa/sidecar-sustained-smoke-post-transition-cache-2026-05-14.md).

This gate runs in ~5-10 seconds and trips the moment any of the
5 cache-wiring callsites in hdmi.rs gets reverted. Companion to
scripts/render_tests.sh (pixel goldens) -- this one's the frame-
budget axis.

Exit codes:
    0   gate passed -- max post-warmup frame_dt <= budget
    1   gate failed -- at least one frame exceeded budget
    2   driver-internal error (subprocess died, missing trace, etc.)

Note: assert on MAX not MEAN. The failure mode is a few catastrophic
frames (cache miss -> font rasterize per frame), not slow average.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from typing import Any


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--binary", required=True, help="Path to openmarquee-render on the Pi")
    p.add_argument("--content-root", required=True, help="Slide content root (item.json lives at <root>/<uuid>/)")
    p.add_argument("--slide-id", required=True, help="UUID of the slide to render")
    p.add_argument("--frames", type=int, default=50, help="How many advance frames to drive (default: 50)")
    p.add_argument("--warmup", type=int, default=3, help="Frame count to ignore at the head (default: 3 -- frame[0] cache cold + mode-set, frame[1] post-init, frame[2] occasional DRM resched; frame[3+] steady state)")
    p.add_argument("--budget-ms", type=float, default=33.0, help="Per-frame budget in ms; gate fails if any post-warmup frame exceeds (default: 33)")
    p.add_argument("--advance-interval-ms", type=int, default=33, help="Wall-clock t_ms increment per advance (default: 33 -- ~30 fps)")
    p.add_argument("--verbose", action="store_true", help="Print every frame's total_us")
    args = p.parse_args()

    # Sanity: warmup must leave at least one frame to assert on.
    if args.warmup >= args.frames:
        print(f"FAIL: warmup={args.warmup} leaves no frames to gate on (frames={args.frames})", file=sys.stderr)
        return 2

    # Collected boundary-trace events from the subprocess's stderr.
    traces: list[dict[str, Any]] = []
    traces_lock = threading.Lock()
    stderr_lines: list[str] = []

    env = os.environ.copy()
    env["OPENMARQUEE_BOUNDARY_TRACE"] = "1"

    proc = subprocess.Popen(
        [args.binary, "--ipc-sidecar"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        text=True,
        env=env,
    )

    def _drain_stderr():
        assert proc.stderr is not None
        for line in iter(proc.stderr.readline, ""):
            line = line.rstrip("\n")
            if not line:
                continue
            if line.startswith('{"trace":"boundary"'):
                try:
                    payload = json.loads(line)
                    with traces_lock:
                        traces.append(payload)
                    continue
                except json.JSONDecodeError:
                    pass
            stderr_lines.append(line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    def send(op: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert proc.stdin is not None and proc.stdout is not None
        req: dict[str, Any] = {"op": op}
        if params is not None:
            req["params"] = params
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            rc = proc.poll()
            raise RuntimeError(f"sidecar EOF on op {op!r} (rc={rc})")
        resp = json.loads(line)
        if "err" in resp:
            raise RuntimeError(f"sidecar err on op {op!r}: {resp['err']}")
        return resp

    rc = 0
    try:
        send("open", {"output": "hdmi", "content_root": args.content_root})
        send("begin_slide", {
            "slide_id": args.slide_id,
            "t0_ms": 0,
            "duration_ms": args.frames * args.advance_interval_ms * 2,  # plenty of room
        })
        for i in range(args.frames):
            send("advance", {"t_ms": i * args.advance_interval_ms})
        # Give the stderr drainer a moment to catch the last lines.
        time.sleep(0.2)
        try:
            send("close", None)
        except Exception:
            pass
    except Exception as e:
        print(f"FAIL: driver error: {e}", file=sys.stderr)
        rc = 2
    finally:
        if proc.stdin and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        stderr_thread.join(timeout=1.0)

    if rc != 0:
        # Already failed during driving; surface the stderr tail for forensics.
        print("--- stderr tail (last 30 lines) ---", file=sys.stderr)
        for line in stderr_lines[-30:]:
            print(line, file=sys.stderr)
        return rc

    # --- Gate analysis ---
    with traces_lock:
        # Filter to the slide-under-test (boundary trace includes slide_id).
        # We only want frames for OUR slide, not any housekeeping ones.
        matching = [t for t in traces if t.get("slide_id") == args.slide_id]

    if len(matching) < args.frames:
        # If we lost trace lines (stderr drainer drop, decode error,
        # binary not built with trace support), can't gate. Surface
        # loudly so an op doesn't paper over a missing trace.
        print(
            f"FAIL: only captured {len(matching)} boundary traces for "
            f"slide {args.slide_id!r}; expected at least {args.frames}.",
            file=sys.stderr,
        )
        print("--- stderr tail (last 30 lines) ---", file=sys.stderr)
        for line in stderr_lines[-30:]:
            print(line, file=sys.stderr)
        return 2

    # Take the first `frames` matching traces (ignore any extras).
    matching = matching[:args.frames]

    # Convert total_us -> total_ms for the budget compare.
    totals_ms = [t["total_us"] / 1000.0 for t in matching]
    post_warmup = totals_ms[args.warmup:]
    max_ms = max(post_warmup)
    mean_ms = sum(post_warmup) / len(post_warmup)
    # Also pull paint_us specifically -- the cache wiring lives at the
    # paint sub-phase, so a regression there is the most informative
    # axis for the trail.
    paint_ms = [t.get("paint_us", 0) / 1000.0 for t in matching]
    paint_post_warmup = paint_ms[args.warmup:]
    paint_max = max(paint_post_warmup)

    if args.verbose:
        for i, t in enumerate(totals_ms):
            marker = "  " if i >= args.warmup else "* "  # * = warmup, excluded
            print(f"{marker}frame[{i:2d}]: total={t:7.2f} ms  paint={paint_ms[i]:7.2f} ms")

    print(
        f"==> {len(matching)} frames driven; warmup={args.warmup}; "
        f"post-warmup max={max_ms:.2f} ms (paint={paint_max:.2f} ms); "
        f"mean={mean_ms:.2f} ms; budget={args.budget_ms:.2f} ms"
    )

    if max_ms > args.budget_ms:
        # Identify the offending frame(s) for actionable error message.
        bad = [
            (i + args.warmup, totals_ms[i + args.warmup], paint_ms[i + args.warmup])
            for i in range(len(post_warmup))
            if post_warmup[i] > args.budget_ms
        ]
        print(
            f"FAIL: {len(bad)} of {len(post_warmup)} post-warmup frames "
            f"exceeded {args.budget_ms:.2f} ms budget. Offenders:",
            file=sys.stderr,
        )
        for idx, total, paint in bad[:5]:
            print(
                f"  frame[{idx}]: total={total:.2f} ms (paint={paint:.2f} ms)",
                file=sys.stderr,
            )
        print(
            "Likely cause: EglSession::slide_caches wire reverted at one "
            "of paint_and_present_one_frame_for_slide / "
            "paint_and_present_one_transition_frame. See 9e776e7 + e6f914e.",
            file=sys.stderr,
        )
        return 1

    print(f"PASS: cache regression gate (max {max_ms:.2f} ms <= {args.budget_ms:.2f} ms budget)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
