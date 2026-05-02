"""Phase 3b — motion renderer perf bench at 1080p sign-native.

Deferred from step 3a (e8f619b / c53ecf0 / a782727) until Tailscale
to qarl's home Pi existed. Now that we're back on office network and
the Pi is reachable, measure the CPU cost of `compose_motion_frame`
with the per-layer bitmap cache enabled, against the spec's 30 fps
budget (33 ms / tick).

Spec budget table (docs/text-layer-motion-spec.md, "Per-frame cost
on Pi Zero 2 W"): at 1080 p sign-native with one animated layer,
ticker / breathe / pulse / blink ~2 ms; bounce / shake ~3 ms.

What this script measures (no DRM, no display — pure compose_motion_
frame CPU time):

  - Slide: SIGN_W × SIGN_H text_slide with N visible layers, all
    motion=ticker/breathe/etc. with text "PERFTEST" at varying box
    positions to defeat any glyph-cache-hit-aliasing.
  - Tick the composer at fixed elapsed_s steps for ~3 seconds wall-
    clock; record per-call ms.
  - Report mean / median / 95th percentile / max for each
    {effect, layer count} combination.

Run on the Pi (no sudo needed — this is CPU only, no /dev/dri):

    cd /home/openmarquee/openmarquee
    PYTHONPATH=backend python3 scripts/phase6_motion_bench.py

The bench prints a markdown table; QA can paste it into a verifier
report. No on-glass output — pure perf data.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(ROOT))

from openmarquee.content import TextBox, TextLayer, TextSlide  # noqa: E402
from openmarquee.motion import (  # noqa: E402
    compose_motion_frame,
    load_motion_background,
    prerender_layer_bitmaps,
)


SIGN_W_DEFAULT = 1920
SIGN_H_DEFAULT = 1080

EFFECTS = ("ticker", "breathe", "pulse", "bounce", "shake", "blink")


def _make_slide(effect: str, n_layers: int, width: int, height: int) -> TextSlide:
    """Build a slide with `n_layers` text layers, all running `effect` at
    intensity=50. Boxes tile horizontally so each layer's glyph bbox sits
    in a distinct region — defeats any unintended PIL glyph-cache aliasing
    that would otherwise let two identical layers share a rasterized
    bitmap."""
    layers = []
    for i in range(n_layers):
        # Tile horizontally with 5 % gap; boxes get smaller as layers
        # increase to keep them inside [0, 1).
        box_w = max(0.1, 0.85 / max(1, n_layers))
        box_x = 0.05 + i * (box_w + 0.01)
        layers.append(
            TextLayer(
                text=f"PT{i}",
                motion=effect,
                motion_intensity=50,
                motion_phase=(i / max(1, n_layers)),
                text_color="#FFFFFF",
                box=TextBox(x=box_x, y=0.1, w=min(box_w, 0.9 - box_x), h=0.8),
            )
        )
    return TextSlide(
        name=f"bench-{effect}-{n_layers}",
        background_color="#001122",
        text_layers=layers,
    )


def bench_one(
    effect: str,
    n_layers: int,
    width: int,
    height: int,
    duration_s: float,
    use_cache: bool,
) -> dict:
    """Time compose_motion_frame for ~`duration_s` seconds. Returns
    a dict with mean / median / p95 / max ms/frame and the iteration
    count."""
    slide = _make_slide(effect, n_layers, width, height)
    bg_cache = load_motion_background(slide, width, height)
    layer_cache = prerender_layer_bitmaps(slide, width, height) if use_cache else None
    samples_ms: list[float] = []
    end_at = time.perf_counter() + duration_s
    elapsed_s = 0.0
    # Tick at 30 Hz wall-equivalent so phase advances naturally; the
    # actual call rate is whatever compose_motion_frame manages.
    tick_step = 1.0 / 30.0
    while time.perf_counter() < end_at:
        t0 = time.perf_counter()
        compose_motion_frame(
            slide, elapsed_s, width, height,
            background_cache=bg_cache,
            layer_bitmap_cache=layer_cache,
        )
        samples_ms.append((time.perf_counter() - t0) * 1000.0)
        elapsed_s += tick_step
    if not samples_ms:
        return {"n": 0, "mean": 0.0, "median": 0.0, "p95": 0.0, "max": 0.0}
    samples_ms.sort()
    p95_idx = max(0, int(len(samples_ms) * 0.95) - 1)
    return {
        "n": len(samples_ms),
        "mean": statistics.fmean(samples_ms),
        "median": statistics.median(samples_ms),
        "p95": samples_ms[p95_idx],
        "max": max(samples_ms),
    }


def _fmt_ms(v: float) -> str:
    return f"{v:6.2f}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--width", type=int, default=SIGN_W_DEFAULT)
    p.add_argument("--height", type=int, default=SIGN_H_DEFAULT)
    p.add_argument(
        "--duration", type=float, default=2.0,
        help="seconds per (effect, n_layers, cache) cell",
    )
    p.add_argument(
        "--layers", type=str, default="1,3",
        help="comma-separated list of layer counts to test",
    )
    p.add_argument(
        "--effects", type=str, default=",".join(EFFECTS),
        help="comma-separated list of effects to test",
    )
    args = p.parse_args()

    layer_counts = [int(x) for x in args.layers.split(",") if x.strip()]
    effects = [e.strip() for e in args.effects.split(",") if e.strip()]
    width, height = args.width, args.height
    budget_ms = 1000.0 / 30.0  # 33.33 ms

    print(f"# motion bench — {width}×{height} sign-native, "
          f"{args.duration}s per cell, 30 fps budget = {budget_ms:.1f} ms\n")
    print("| effect  | layers | cache | n     | mean   | median | p95    | max    | over budget? |")
    print("|---------|--------|-------|-------|--------|--------|--------|--------|--------------|")

    for effect in effects:
        for n_layers in layer_counts:
            for use_cache in (True, False):
                r = bench_one(
                    effect, n_layers, width, height,
                    args.duration, use_cache,
                )
                over = "**YES**" if r["p95"] > budget_ms else "no"
                cache_label = "cache" if use_cache else "cold"
                print(
                    f"| {effect:<7} | {n_layers:>6} | {cache_label:<5} | "
                    f"{r['n']:>5} | {_fmt_ms(r['mean'])} | "
                    f"{_fmt_ms(r['median'])} | {_fmt_ms(r['p95'])} | "
                    f"{_fmt_ms(r['max'])} | {over:>12} |"
                )
    print("\n(p95 = 95th percentile; over-budget means at-30-fps tick budget exceeded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
