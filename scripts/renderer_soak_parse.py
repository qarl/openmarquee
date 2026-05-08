#!/usr/bin/env python3
"""Parse [mem] lines from the soak log; assert no monotonic growth.

Spec ref: docs/renderer-rewrite-requirements.md §8.2 (no-leak), §11
(V1-GA acceptance test). Canonical sample stream is the per-pass
[mem] line emitted by render_playlist_reel:

  [mem] pass=N vm_rss=NN.NMB vm_data=NN.NMB swap=NN.NMB cma_used=NN.NMB
        bo=N fb=N fbo=N textures=N

The slope test fits a least-squares line through pass-indexed RSS /
VmData / CmaUsed values and asserts the slope (converted to MB/hour
assuming ~30 s/pass on the FYS reel) is below the configured ceiling.
A growing slope catches monotonic leaks; transient spikes don't move
the regression slope much.

The "MB per hour" units take a slight liberty -- pass duration varies
with hold/transition timings, so the per-hour conversion is nominal.
The slope value in MB/pass is the ground-truth metric; per-hour is
the human-friendly framing.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import List, Sequence


PAT = re.compile(
    r"^\[mem\]\s+(\S+)\s+"
    r"vm_rss=([\d.]+)MB\s+"
    r"vm_data=([\d.]+)MB\s+"
    r"swap=([\d.]+)MB\s+"
    r"cma_used=([\d.]+)MB"
)


def slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Least-squares slope of y over x. Returns 0.0 for <3 points or
    constant x."""
    n = len(xs)
    if n < 3:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else 0.0


def parse_log(path: str) -> List[dict]:
    """Pull every [mem] line, keeping only pass=N samples. session=
    open/close are noise for the slope test (one value each)."""
    out: List[dict] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = PAT.match(line.strip())
            if not m:
                continue
            label = m.group(1)
            if not label.startswith("pass="):
                continue
            pass_n = int(label.split("=", 1)[1])
            out.append(
                {
                    "pass": pass_n,
                    "rss": float(m.group(2)),
                    "data": float(m.group(3)),
                    "swap": float(m.group(4)),
                    "cma": float(m.group(5)),
                }
            )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("log", help="path to soak run stderr capture")
    ap.add_argument("--max-slope-mb-per-hour", type=float, default=5.0,
                    help="MB/hour ceiling for VmRSS / VmData / CmaUsed slope")
    ap.add_argument("--max-cma-mb", type=float, default=200.0,
                    help="hard ceiling for end-of-soak CmaUsed (budget §4)")
    ap.add_argument("--max-rss-mb", type=float, default=100.0,
                    help="hard ceiling for end-of-soak VmRSS (budget §4)")
    ap.add_argument("--passes-per-hour", type=float, default=120.0,
                    help="nominal passes/hour for slope→hour conversion "
                         "(default 120 = 30s/pass on FYS reel)")
    args = ap.parse_args()

    samples = parse_log(args.log)
    if len(samples) < 3:
        print(
            f"FAIL: insufficient pass=N samples "
            f"({len(samples)} < 3) -- soak too short or log malformed",
            file=sys.stderr,
        )
        return 1

    passes = [s["pass"] for s in samples]
    rss_slope_pp = slope(passes, [s["rss"] for s in samples])
    data_slope_pp = slope(passes, [s["data"] for s in samples])
    swap_slope_pp = slope(passes, [s["swap"] for s in samples])
    cma_slope_pp = slope(passes, [s["cma"] for s in samples])

    rss_per_hour = rss_slope_pp * args.passes_per_hour
    data_per_hour = data_slope_pp * args.passes_per_hour
    swap_per_hour = swap_slope_pp * args.passes_per_hour
    cma_per_hour = cma_slope_pp * args.passes_per_hour

    print(f"samples={len(samples)} passes={passes[0]}..{passes[-1]}")
    print(
        f"  vm_rss   slope={rss_slope_pp:+.3f} MB/pass = "
        f"{rss_per_hour:+.1f} MB/hour"
    )
    print(
        f"  vm_data  slope={data_slope_pp:+.3f} MB/pass = "
        f"{data_per_hour:+.1f} MB/hour"
    )
    print(
        f"  swap     slope={swap_slope_pp:+.3f} MB/pass = "
        f"{swap_per_hour:+.1f} MB/hour"
    )
    print(
        f"  cma_used slope={cma_slope_pp:+.3f} MB/pass = "
        f"{cma_per_hour:+.1f} MB/hour"
    )

    # Final-sample summary for budget assertion
    last = samples[-1]
    print(
        f"  final pass={last['pass']} rss={last['rss']:.1f}MB "
        f"cma={last['cma']:.1f}MB swap={last['swap']:.1f}MB"
    )

    failures: List[str] = []
    for name, val in (
        ("vm_rss", rss_per_hour),
        ("vm_data", data_per_hour),
        ("swap", swap_per_hour),
        ("cma_used", cma_per_hour),
    ):
        if val > args.max_slope_mb_per_hour:
            failures.append(
                f"{name} slope {val:+.1f} MB/hour > "
                f"{args.max_slope_mb_per_hour} MB/hour ceiling"
            )

    if last["cma"] > args.max_cma_mb:
        failures.append(
            f"final cma_used {last['cma']:.1f}MB > "
            f"{args.max_cma_mb}MB hard ceiling (budget §4)"
        )
    if last["rss"] > args.max_rss_mb:
        failures.append(
            f"final vm_rss {last['rss']:.1f}MB > "
            f"{args.max_rss_mb}MB hard ceiling (budget §4)"
        )

    if failures:
        for f in failures:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1

    print("PASS: no monotonic growth detected; budget intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
