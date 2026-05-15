#!/usr/bin/env python3
"""Phase 3k pattern-shader diagnostic for parity_animated_stripes_bounce.

Worst-SSIM fixture in the parity gate as of bde81b6 (0.5715 vs gate
threshold 0.95). Background_pattern=stripes + motion=bounce; the
text layer is a small "STRIPES" headline. The pattern dominates the
frame area, so most disagreement pixels come from the pattern
shader, not the text.

Probe applies the Phase-1c playbook (5759b1e + 9fc2206 precedent):
read Canvas2D capture + Rust golden, per-pixel decompose, emit:

  qa/captures/stripes-canvas2d.png   (just the C2D side, cropped)
  qa/captures/stripes-rust.png       (just the Rust golden, cropped)
  qa/captures/stripes-diff.png       (delta heatmap)
  qa/captures/stripes-sxs.png        (4-up side-by-side with gutter)
  qa/captures/stripes-diag.json      (per-channel max, per-quadrant
                                       mean, histogram, cluster
                                       bounding box)

No production source change. Pure data dump for the next dispatch.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
CANVAS_PNG = REPO / "renderer" / "tests" / "parity" / "captures" / "parity_animated_stripes_bounce.browser.png"
GOLDEN_PNG = REPO / "renderer" / "tests" / "golden" / "animated_stripes_bounce.png"
OUT_DIR = REPO / "qa" / "captures"


def main():
    from PIL import Image
    import numpy as np

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("=== Phase 3k stripes-bounce diagnostic ===\n", file=sys.stderr)

    canvas_im = Image.open(CANVAS_PNG).convert("RGBA")
    rust_im = Image.open(GOLDEN_PNG).convert("RGBA")
    if canvas_im.size != rust_im.size:
        print(f"FAIL: size mismatch canvas={canvas_im.size} rust={rust_im.size}",
              file=sys.stderr)
        sys.exit(2)

    canvas = np.array(canvas_im, dtype=np.int16)  # (H, W, 4)
    rust = np.array(rust_im, dtype=np.int16)
    h, w = canvas.shape[:2]

    delta = np.abs(canvas - rust)  # (H, W, 4)
    delta_max = delta.max(axis=2)  # per-pixel max across RGBA

    # Per-channel max + mean.
    per_chan_max = [int(delta[..., c].max()) for c in range(4)]
    per_chan_mean = [float(delta[..., c].mean()) for c in range(4)]

    # Per-quadrant mean (delta_max). H/2 x W/2 quadrants TL/TR/BL/BR.
    hm = h // 2
    wm = w // 2
    quads = {
        "TL": float(delta_max[:hm, :wm].mean()),
        "TR": float(delta_max[:hm, wm:].mean()),
        "BL": float(delta_max[hm:, :wm].mean()),
        "BR": float(delta_max[hm:, wm:].mean()),
    }

    # Histogram of delta values (5 buckets).
    edges = [0, 1, 3, 11, 51, 256]  # rightmost included
    flat = delta_max.flatten()
    hist = {}
    bucket_labels = ["0", "1-2", "3-10", "11-50", "51-255"]
    for i, label in enumerate(bucket_labels):
        lo, hi = edges[i], edges[i + 1]
        if i == 0:
            cnt = int(((flat >= lo) & (flat < hi + 1)).sum() - (flat == 0).sum() * 0 + (flat == 0).sum())
            cnt = int((flat == 0).sum())
        elif label == "1-2":
            cnt = int(((flat >= 1) & (flat <= 2)).sum())
        elif label == "3-10":
            cnt = int(((flat >= 3) & (flat <= 10)).sum())
        elif label == "11-50":
            cnt = int(((flat >= 11) & (flat <= 50)).sum())
        else:
            cnt = int((flat >= 51).sum())
        hist[label] = cnt

    # Cluster bounding box of "loud" pixels (delta > 50).
    loud_ys, loud_xs = np.where(delta_max > 50)
    if len(loud_xs) > 0:
        bbox = {
            "x_min": int(loud_xs.min()),
            "x_max": int(loud_xs.max()),
            "y_min": int(loud_ys.min()),
            "y_max": int(loud_ys.max()),
            "count": int(len(loud_xs)),
        }
    else:
        bbox = {"count": 0}

    # Stripes have a periodic structure. Sample a horizontal line at
    # y = h//2 from both images and report (rust_at_x, canvas_at_x)
    # at the first ~30 x positions to see the phase + period.
    row_mid = h // 2
    samples = []
    for x in range(0, min(60, w), 2):
        rust_px = [int(rust[row_mid, x, c]) for c in range(3)]
        canvas_px = [int(canvas[row_mid, x, c]) for c in range(3)]
        samples.append({"x": x, "rust_rgb": rust_px, "canvas_rgb": canvas_px})

    # Phase 3l-prep multi-row probe: find the first stripe transition
    # x position in BOTH renderers at 3 y rows. Disambiguates
    # top-left-anchor model (constant offset across y) vs CSS-axis-
    # center model (offset linear in (y - center_y)) vs other.
    #
    # "Transition x" defined as the first x >= 0 where the pixel's
    # G channel crosses the midpoint between color_a.G (=26) and
    # color_b.G (=180), i.e. >=103. Using G is the cleanest signal
    # since color_a=[94,26,26] and color_b=[255,180,60] differ most
    # in G (154 vs 60 in B). Sub-pixel: linearly interpolate
    # between the bracketing pixels for AA-ramped Canvas2D so the
    # transition x captures the sub-pixel center, not the integer
    # bracket. Rust is a hard step so the same lerp degenerates to
    # whichever pixel-boundary crossed 103 first.
    MID_G = 103  # midpoint of color_a.G=26 and color_b.G=180
    def find_transition_x(arr_2d, y):
        row = arr_2d[y, :, 1]  # G channel of row y, all x
        # Walk from x=0 to find first crossing low->high.
        # Stop at w/2 -- we just want the FIRST transition.
        for x in range(1, min(w // 2, w)):
            if row[x - 1] < MID_G <= row[x]:
                # Sub-pixel: linear interp between bracketing pixels.
                g_lo, g_hi = float(row[x - 1]), float(row[x])
                if g_hi == g_lo:
                    return float(x)
                frac = (MID_G - g_lo) / (g_hi - g_lo)
                return float(x - 1) + frac
        return None
    rows_to_probe = [270, 540, 810]
    multirow = []
    for y in rows_to_probe:
        rust_tx = find_transition_x(rust, y)
        canvas_tx = find_transition_x(canvas, y)
        offset = (rust_tx - canvas_tx) if (rust_tx is not None and canvas_tx is not None) else None
        multirow.append({
            "y": y,
            "rust_transition_x": rust_tx,
            "canvas2d_transition_x": canvas_tx,
            "offset_rust_minus_canvas": offset,
        })
    # Verdict heuristic: the raw "first low->high" offset is unreliable
    # because each row may start in different stripe-zone (e.g., row
    # y=810 starts in color_b for Canvas2D but color_a for Rust due
    # to phase wrap). Take the offset MODULO the x-period (which is
    # tile*sqrt(2) for 45deg stripes -- empirically ~86 px for
    # tile=61 from density_curve(0.5)). The signed-minimum residue
    # is the true phase offset along x.
    X_PERIOD = 61.0 * 1.41421356  # 45deg x-period for tile=61
    def signed_mod(v, p):
        # Map v into (-p/2, p/2].
        r = v - p * round(v / p)
        return r
    valid = [r for r in multirow if r["offset_rust_minus_canvas"] is not None]
    if len(valid) == 3:
        normalized = [signed_mod(r["offset_rust_minus_canvas"], X_PERIOD)
                      for r in valid]
        for r, n in zip(valid, normalized):
            r["offset_normalized_signed"] = n
        max_spread = max(normalized) - min(normalized)
        if max_spread < 1.0:
            verdict = (f"top-left-anchor (constant offset ~{sum(normalized)/3:+.2f} px along x, "
                      f"spread {max_spread:.2f}). Phase 3l fix shape: 1-line shader "
                      f"phase shift by ~{(sum(normalized)/3)/1.41421356:+.2f} along the 45deg axis.")
        elif max_spread < 5.0:
            verdict = (f"approx-constant offset ~{sum(normalized)/3:+.2f}+-{max_spread/2:.2f} px "
                      f"along x. Likely top-left-anchor with AA-sampling noise.")
        else:
            verdict = (f"OTHER -- normalized offsets {[round(n,2) for n in normalized]} "
                      f"vary by {max_spread:.2f} px. Geometry mechanism non-obvious.")
    else:
        verdict = "INCONCLUSIVE: one or more rows had no transition"

    out = {
        "fixture": "parity_animated_stripes_bounce",
        "dims": [w, h],
        "per_channel_max": {"r": per_chan_max[0], "g": per_chan_max[1],
                            "b": per_chan_max[2], "a": per_chan_max[3]},
        "per_channel_mean": {"r": per_chan_mean[0], "g": per_chan_mean[1],
                             "b": per_chan_mean[2], "a": per_chan_mean[3]},
        "delta_max_overall": int(delta_max.max()),
        "delta_max_mean": float(delta_max.mean()),
        "per_quadrant_mean_delta_max": quads,
        "histogram_delta_max": hist,
        "loud_pixel_bbox": bbox,
        "row_mid_samples_first60x_step2": samples,
        "multirow_transitions": multirow,
        "multirow_verdict": verdict,
    }
    (OUT_DIR / "stripes-diag.json").write_text(json.dumps(out, indent=2))

    # Diff heatmap PNG: red=loud, green=mild, gray=zero.
    diff_rgb = np.zeros((h, w, 3), dtype=np.uint8)
    mild = (delta_max >= 1) & (delta_max <= 10)
    loud = delta_max > 10
    diff_rgb[mild] = [0, 200, 0]
    diff_rgb[loud, 0] = np.minimum(255, delta_max[loud] * 2).astype(np.uint8)
    diff_rgb[loud, 1] = 0
    diff_rgb[loud, 2] = 0
    Image.fromarray(diff_rgb, mode="RGB").save(OUT_DIR / "stripes-diff.png")

    # Crops of the two sides at the same region for visual eyeballing.
    canvas_im.save(OUT_DIR / "stripes-canvas2d.png")
    rust_im.save(OUT_DIR / "stripes-rust.png")

    # SxS quad with 4px gutter: [C2D | gutter | Rust | gutter | Diff].
    gutter = 4
    sxs_w = w * 3 + gutter * 2
    sxs = np.full((h, sxs_w, 3), 32, dtype=np.uint8)  # dark gray gutter
    sxs[:, :w] = np.array(canvas_im.convert("RGB"))
    sxs[:, w + gutter:2 * w + gutter] = np.array(rust_im.convert("RGB"))
    sxs[:, 2 * w + 2 * gutter:] = diff_rgb
    Image.fromarray(sxs, mode="RGB").save(OUT_DIR / "stripes-sxs.png")

    # Print summary to stderr.
    print(f"image dims:                  {w}x{h}", file=sys.stderr)
    print(f"per-channel max R/G/B/A:     {per_chan_max}", file=sys.stderr)
    print(f"per-channel mean R/G/B/A:    {[round(m,3) for m in per_chan_mean]}", file=sys.stderr)
    print(f"delta_max overall / mean:    {out['delta_max_overall']} / {out['delta_max_mean']:.3f}",
          file=sys.stderr)
    print("per-quadrant mean delta_max:", file=sys.stderr)
    for k, v in quads.items():
        print(f"  {k}: {v:.3f}", file=sys.stderr)
    print("histogram (delta_max):", file=sys.stderr)
    for label, cnt in hist.items():
        pct = 100.0 * cnt / (h * w)
        print(f"  {label:6}: {cnt:9d}  ({pct:.2f}%)", file=sys.stderr)
    print(f"loud-pixel bbox (delta>50): {bbox}", file=sys.stderr)
    print("\nMulti-row transition probe (Phase 3l-prep):", file=sys.stderr)
    for r in multirow:
        rt = r["rust_transition_x"]
        ct = r["canvas2d_transition_x"]
        off = r["offset_rust_minus_canvas"]
        rt_s = f"{rt:7.3f}" if rt is not None else "  none "
        ct_s = f"{ct:7.3f}" if ct is not None else "  none "
        off_s = f"{off:+7.3f}" if off is not None else "  none "
        print(f"  y={r['y']:4d}  rust_x={rt_s}  canvas_x={ct_s}  offset={off_s}",
              file=sys.stderr)
    print(f"verdict: {verdict}", file=sys.stderr)
    print(f"\nartifacts: {OUT_DIR / 'stripes-*.png'}, stripes-diag.json", file=sys.stderr)


if __name__ == "__main__":
    main()
