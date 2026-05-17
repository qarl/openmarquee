#!/usr/bin/env python3
"""Parse `ipc.soak` lines from a journalctl capture; assert §11 acceptance.

Phase 9 Step 9b (2026-05-16): companion parser to renderer_pi_soak_ipc.sh.
The Rust IPC sidecar emits one structured line every 30s aggregating
per-Advance paint timings (added in commit ffbb437, Phase 9 Step 9a):

  <journalctl prefix> ipc.soak window_s=W frames=F transitions=T
                      fps_avg=A.A paint_us=avg/U/max/M
                      session_frames=SF session_transitions=ST

This parser:
  1. Scans the journalctl capture for ipc.soak lines.
  2. Detects OOM signals (kernel "Out of memory" lines, openmarquee-render
     killed-by-signal, backend service Main process exit).
  3. Computes total + windowed stats: cumulative frames/transitions,
     overall avg fps, min fps over any 10-min rolling window, max
     paint_us across the soak.
  4. Asserts §11 acceptance:
       - min rolling fps >= --min-fps-avg (default 30.0)
       - no OOM signals
       - no backend crash signals
  5. Emits a human-readable summary to stdout + (optional) machine-readable
     JSON to --json PATH.

Exit code: 0 on PASS, non-zero on FAIL (slope/budget/§11 gate failures).

Spec ref: docs/renderer-rewrite-requirements.md §11 (V1-GA acceptance).
Companion: scripts/renderer_soak_parse.py handles the standalone-reel
[mem] slope gate (§8.2 no-leak). Both can run on the same journalctl
capture: this script gates fps + OOM; the other gates mem slope.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import List, Optional

# Anchor token "ipc.soak" then key=value pairs. New fields go on the
# right per the format contract documented at IpcPaintMetrics in
# renderer/src/ipc_main.rs; regex is non-positional + non-greedy so
# unknown trailing tokens are ignored.
#
# Phase D slice 2 (2026-05-17) adds the optional `paint_us_p99` group.
# Optional so pre-slice-1 captures (no p99 field) still parse; the
# p99 gate skips with a one-line warning in that backward-compat
# path.
PAT_IPC_SOAK = re.compile(
    r"ipc\.soak\s+"
    r"window_s=(?P<window_s>\d+)\s+"
    r"frames=(?P<frames>\d+)\s+"
    r"transitions=(?P<transitions>\d+)\s+"
    r"fps_avg=(?P<fps_avg>[\d.]+)\s+"
    r"paint_us=avg/(?P<paint_us_avg>\d+)/max/(?P<paint_us_max>\d+)\s+"
    r"(?:paint_us_p99=(?P<paint_us_p99>\d+)\s+)?"
    r"session_frames=(?P<session_frames>\d+)\s+"
    r"session_transitions=(?P<session_transitions>\d+)"
)

# OOM / crash signal patterns.
PAT_OOM = re.compile(
    r"(Out of memory|oom-killer|Killed process \d+ \(openmarquee-render\))",
    re.IGNORECASE,
)
PAT_BACKEND_EXIT = re.compile(
    r"openmarquee-backend\.service.*(Main process exited|Failed with result)",
    re.IGNORECASE,
)


def parse_log(path: str) -> dict:
    """Read the journalctl capture once; emit a dict of parsed signals."""
    samples: List[dict] = []
    oom_hits: List[str] = []
    crash_hits: List[str] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = PAT_IPC_SOAK.search(line)
            if m:
                p99_raw = m.group("paint_us_p99")
                samples.append({
                    "window_s": int(m.group("window_s")),
                    "frames": int(m.group("frames")),
                    "transitions": int(m.group("transitions")),
                    "fps_avg": float(m.group("fps_avg")),
                    "paint_us_avg": int(m.group("paint_us_avg")),
                    "paint_us_max": int(m.group("paint_us_max")),
                    "paint_us_p99": int(p99_raw) if p99_raw is not None else None,
                    "session_frames": int(m.group("session_frames")),
                    "session_transitions": int(m.group("session_transitions")),
                })
                continue
            if PAT_OOM.search(line):
                oom_hits.append(line.strip())
            elif PAT_BACKEND_EXIT.search(line):
                crash_hits.append(line.strip())
    return {
        "samples": samples,
        "oom_hits": oom_hits,
        "crash_hits": crash_hits,
    }


def rolling_min_fps(samples: List[dict], window_min: int) -> Optional[dict]:
    """Sliding window over consecutive samples; return the window with
    lowest avg fps (computed as total_frames / total_window_seconds).
    Skips windows where the elapsed seconds is < window_min * 60 (so
    short tails don't dominate).

    Returns dict with start_idx, end_idx, fps, total_frames, elapsed_s
    or None if no qualifying window exists.
    """
    if not samples:
        return None
    target_s = window_min * 60
    best: Optional[dict] = None
    n = len(samples)
    for start in range(n):
        elapsed = 0
        frames = 0
        for end in range(start, n):
            elapsed += samples[end]["window_s"]
            # frames in this sub-window = sum of per-window frames.
            # Note: paint records only successful paints, so a window
            # with rendering pauses naturally shows lower fps.
            frames += samples[end]["frames"] + samples[end]["transitions"]
            if elapsed >= target_s:
                fps = frames / elapsed if elapsed > 0 else 0.0
                if best is None or fps < best["fps"]:
                    best = {
                        "start_idx": start,
                        "end_idx": end,
                        "fps": fps,
                        "total_frames": frames,
                        "elapsed_s": elapsed,
                    }
                break  # advance the start; this end has the smallest qualifying window
    return best


def rolling_max_p99(samples: List[dict], window_min: int) -> Optional[dict]:
    """Sliding window over consecutive samples; return the window with
    the highest per-sample paint_us_p99 (the worst-case sliding
    window for the slice-2 p99 gate). Modeled after rolling_min_fps.

    Samples missing paint_us_p99 (pre-slice-1 captures) are skipped
    in the per-window max. If ALL samples lack the field, returns
    None — the caller treats this as "no p99 data, skip the gate"
    for backward compatibility.

    Returns dict with start_idx, end_idx, max_p99, elapsed_s, or
    None if no qualifying window exists or no samples carry p99.
    """
    if not samples:
        return None
    if not any(s.get("paint_us_p99") is not None for s in samples):
        return None
    target_s = window_min * 60
    best: Optional[dict] = None
    n = len(samples)
    for start in range(n):
        elapsed = 0
        window_max_p99 = 0
        for end in range(start, n):
            elapsed += samples[end]["window_s"]
            p99 = samples[end].get("paint_us_p99")
            if p99 is not None and p99 > window_max_p99:
                window_max_p99 = p99
            if elapsed >= target_s:
                if best is None or window_max_p99 > best["max_p99"]:
                    best = {
                        "start_idx": start,
                        "end_idx": end,
                        "max_p99": window_max_p99,
                        "elapsed_s": elapsed,
                    }
                break
    return best


def summarize(
    parsed: dict,
    min_fps: float,
    rolling_window_min: int,
    max_p99_paint_us: int,
) -> dict:
    samples = parsed["samples"]
    oom_hits = parsed["oom_hits"]
    crash_hits = parsed["crash_hits"]

    failures: List[str] = []

    if not samples:
        failures.append("no ipc.soak samples found in log (sidecar not running, or window <30s)")
        # Preserve the additive-keys invariant on the empty-samples
        # early-return path so callers iterating the JSON don't
        # KeyError on the new Phase D slice 2 fields.
        return {
            "pass": False,
            "failures": failures,
            "warnings": [],
            "samples": 0,
            "paint_us_p99_max": None,
            "paint_us_p99_violations": 0,
            "rolling_max_p99": None,
            "oom_hits": oom_hits,
            "crash_hits": crash_hits,
        }

    total_window_s = sum(s["window_s"] for s in samples)
    total_frames = sum(s["frames"] for s in samples)
    total_transitions = sum(s["transitions"] for s in samples)
    total_paints = total_frames + total_transitions
    overall_fps = total_paints / total_window_s if total_window_s > 0 else 0.0

    # Max paint_us across the whole soak.
    max_paint_us = max(s["paint_us_max"] for s in samples)

    # Session counters: last sample's session_* shows total since
    # session=open. Use it as a cross-check on the windowed sum
    # (should agree modulo dropped samples / journalctl truncation).
    last_session_frames = samples[-1]["session_frames"]
    last_session_transitions = samples[-1]["session_transitions"]

    rolling = rolling_min_fps(samples, rolling_window_min)

    # §11 gate.
    if rolling is None:
        # Soak shorter than rolling_window_min; fall back to overall.
        if overall_fps < min_fps:
            failures.append(
                f"overall fps {overall_fps:.2f} < {min_fps:.2f} floor "
                f"(soak too short for {rolling_window_min}-min rolling window)"
            )
    else:
        if rolling["fps"] < min_fps:
            failures.append(
                f"min rolling fps over {rolling_window_min}min window "
                f"= {rolling['fps']:.2f} < {min_fps:.2f} floor "
                f"(window samples [{rolling['start_idx']}..{rolling['end_idx']}], "
                f"{rolling['total_frames']} paints over {rolling['elapsed_s']}s)"
            )
    # Phase D slice 2: p99 gate. Sample-level p99 absent on
    # pre-slice-1 captures; rolling_max_p99 returns None in that
    # path. Backward-compat: skip the p99 gate with a warning
    # surfaced to the human report (not a failure).
    rolling_p99 = rolling_max_p99(samples, rolling_window_min)
    p99_warnings: List[str] = []
    p99_max_observed = max(
        (s["paint_us_p99"] for s in samples if s.get("paint_us_p99") is not None),
        default=None,
    )
    p99_violations = sum(
        1
        for s in samples
        if s.get("paint_us_p99") is not None and s["paint_us_p99"] > max_p99_paint_us
    )
    if rolling_p99 is None:
        p99_warnings.append(
            "paint_us_p99 absent in all ipc.soak samples — running "
            "fps-only gate; pre-Phase-D-slice-1 binary"
        )
    else:
        if rolling_p99["max_p99"] > max_p99_paint_us:
            failures.append(
                f"max rolling p99 paint_us over {rolling_window_min}min window "
                f"= {rolling_p99['max_p99']} > {max_p99_paint_us} budget "
                f"(window samples [{rolling_p99['start_idx']}..{rolling_p99['end_idx']}], "
                f"{rolling_p99['elapsed_s']}s)"
            )

    if oom_hits:
        failures.append(
            f"OOM signal detected ({len(oom_hits)} hits); first: {oom_hits[0]!r}"
        )
    if crash_hits:
        failures.append(
            f"backend crash signal detected ({len(crash_hits)} hits); first: {crash_hits[0]!r}"
        )

    return {
        "pass": not failures,
        "failures": failures,
        "warnings": p99_warnings,
        "samples": len(samples),
        "total_window_s": total_window_s,
        "total_frames": total_frames,
        "total_transitions": total_transitions,
        "overall_fps": overall_fps,
        "max_paint_us": max_paint_us,
        "paint_us_p99_max": p99_max_observed,
        "paint_us_p99_violations": p99_violations,
        "session_frames": last_session_frames,
        "session_transitions": last_session_transitions,
        "rolling_min_fps": rolling,
        "rolling_max_p99": rolling_p99,
        "oom_hits": oom_hits,
        "crash_hits": crash_hits,
    }


def print_human_report(summary: dict, min_fps: float, rolling_window_min: int) -> None:
    print(f"samples={summary['samples']}")
    if summary["samples"] == 0:
        return
    print(
        f"  total_window_s={summary['total_window_s']} "
        f"total_paints={summary['total_frames']}+{summary['total_transitions']} "
        f"overall_fps={summary['overall_fps']:.2f}"
    )
    print(
        f"  max_paint_us={summary['max_paint_us']} "
        f"session_frames={summary['session_frames']} "
        f"session_transitions={summary['session_transitions']}"
    )
    rolling = summary.get("rolling_min_fps")
    if rolling:
        print(
            f"  rolling_min_fps ({rolling_window_min}min): {rolling['fps']:.2f} "
            f"(samples [{rolling['start_idx']}..{rolling['end_idx']}], "
            f"{rolling['total_frames']} paints / {rolling['elapsed_s']}s)"
        )
    else:
        print(f"  rolling window: N/A (soak shorter than {rolling_window_min}min)")
    rolling_p99 = summary.get("rolling_max_p99")
    if rolling_p99:
        print(
            f"  rolling_max_p99 ({rolling_window_min}min): "
            f"{rolling_p99['max_p99']}us "
            f"(samples [{rolling_p99['start_idx']}..{rolling_p99['end_idx']}], "
            f"{rolling_p99['elapsed_s']}s)"
        )
        if summary.get("paint_us_p99_max") is not None:
            print(
                f"  paint_us_p99_max={summary['paint_us_p99_max']} "
                f"paint_us_p99_violations={summary['paint_us_p99_violations']}"
            )
    for warn in summary.get("warnings", []):
        print(f"  WARN: {warn}")
    if summary["oom_hits"]:
        print(f"  OOM hits: {len(summary['oom_hits'])}")
    if summary["crash_hits"]:
        print(f"  crash hits: {len(summary['crash_hits'])}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("log", help="path to journalctl capture from openmarquee-backend")
    ap.add_argument(
        "--min-fps-avg", type=float, default=30.0,
        help="§11 fps floor. Default 30.0; QA can relax for diagnostic runs.",
    )
    ap.add_argument(
        "--rolling-window-min", type=int, default=10,
        help="rolling window length in minutes for the min-fps + max-p99 "
             "gates. Default 10. A single bad 30s window doesn't fail; "
             "a 10-min sag does.",
    )
    ap.add_argument(
        "--max-p99-paint-us", type=int, default=33333,
        help="Phase D slice 2 (2026-05-17): per-frame p99 paint budget "
             "in microseconds. Default 33333 = the literal 30 fps "
             "frame budget (1s / 30 = 33.33ms). Pre-slice-1 captures "
             "without paint_us_p99 skip this gate with a warning.",
    )
    ap.add_argument(
        "--json", type=str, default=None,
        help="optional path to write the parsed summary as JSON.",
    )
    args = ap.parse_args()

    parsed = parse_log(args.log)
    summary = summarize(
        parsed,
        args.min_fps_avg,
        args.rolling_window_min,
        args.max_p99_paint_us,
    )

    print_human_report(summary, args.min_fps_avg, args.rolling_window_min)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, default=str)

    if not summary["pass"]:
        print("", file=sys.stderr)
        for fmsg in summary["failures"]:
            print(f"FAIL: {fmsg}", file=sys.stderr)
        return 1

    print("")
    print("PASS: §11 acceptance criteria met (fps floor + no OOM/crash)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
