#!/usr/bin/env python3
"""Phase 3s-prep: spatial diag for parity_bg_pattern_dots.

Hypothesis from code-reading:
- Canvas2D (ui/src/bg-system.js:272-283) uses ctx.arc + ctx.fill -> browser
  bilinear AA produces a ~1-px smooth ring at every circle boundary.
- Rust (FS_PATTERN_DOTS in renderer/src/hdmi_logic.rs:2255) uses
  step(d2, r2) which is HARD -- no AA at all.

The divergence should manifest as a thin AA-ring on EVERY dot boundary
in the canvas. This probe captures:
  - canvas2d + rust + diff PNGs
  - max_delta + mean_delta + per-channel
  - histogram of deltas (where the AA energy clusters)
  - a 1-tile crop around a single dot to eyeball the AA-ring directly

Outputs:
  qa/captures/dots-canvas2d.png       (Canvas2D capture, copied from parity)
  qa/captures/dots-rust.png           (Rust golden, copied)
  qa/captures/dots-diff.png           (per-pixel max-channel delta, x4 amplified)
  qa/captures/dots-sxs.png            (side-by-side at 4x)
  qa/captures/dots-tile-crop.png      (single-tile crop showing AA ring)
  qa/captures/dots-diag-summary.json  (numeric metrics)
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent.parent
BROWSER = REPO / "renderer" / "tests" / "parity" / "captures" / "parity_bg_pattern_dots.browser.png"
GOLDEN = REPO / "renderer" / "tests" / "golden" / "bg_pattern_dots.png"
OUT_DIR = REPO / "qa" / "captures"

# parity_bg_pattern_dots: density=0.5 -> tile = round(lerp(48, 4, 0.5)) = 26
TILE = 26
RADIUS = 6  # round(26 * 0.22) = round(5.72) = 6


def main():
    b = np.asarray(Image.open(BROWSER).convert("RGB"), dtype=np.int16)
    g = np.asarray(Image.open(GOLDEN).convert("RGB"), dtype=np.int16)
    if b.shape != g.shape:
        b_resized = Image.open(BROWSER).convert("RGB").resize((g.shape[1], g.shape[0]), Image.LANCZOS)
        b = np.asarray(b_resized, dtype=np.int16)
    h, w, _ = g.shape
    print(f"Image dims: {w} x {h}")

    diff = np.abs(b - g)  # h x w x 3
    max_chan = diff.max(axis=2)  # h x w

    print(f"Max delta any-channel:     {int(max_chan.max())}")
    print(f"Mean delta any-channel:    {float(max_chan.mean()):.3f}")
    print(f"Pixels with delta>=10:     {int((max_chan >= 10).sum())}  ({100*(max_chan>=10).mean():.3f}%)")
    print(f"Pixels with delta>=50:     {int((max_chan >= 50).sum())}  ({100*(max_chan>=50).mean():.3f}%)")
    print(f"Pixels with delta>=100:    {int((max_chan >= 100).sum())}  ({100*(max_chan>=100).mean():.3f}%)")
    print(f"Pixels with delta>=200:    {int((max_chan >= 200).sum())}  ({100*(max_chan>=200).mean():.3f}%)")

    # Per-channel max
    for ci, cname in enumerate(["R", "G", "B"]):
        print(f"  Max delta {cname}: {int(diff[..., ci].max())}, mean {diff[..., ci].mean():.3f}")

    # Per-quadrant mean
    print("\nPer-quadrant mean (loud means more divergence in that quadrant):")
    for qy, qy_name in enumerate(["top", "bottom"]):
        for qx, qx_name in enumerate(["left", "right"]):
            y0, y1 = (0, h // 2) if qy == 0 else (h // 2, h)
            x0, x1 = (0, w // 2) if qx == 0 else (w // 2, w)
            qm = max_chan[y0:y1, x0:x1].mean()
            print(f"  {qy_name:6s}-{qx_name:6s}: mean={qm:.3f}")

    # Histogram of deltas
    print("\nHistogram of max-channel delta (buckets):")
    hist, edges = np.histogram(max_chan, bins=[0, 5, 10, 25, 50, 100, 150, 200, 256])
    for i in range(len(hist)):
        pct = 100 * hist[i] / max_chan.size
        print(f"  [{edges[i]:3d}, {edges[i+1]:3d}):  {hist[i]:>8d}  ({pct:.3f}%)")

    # Tile crop: find first dot location (top-left dot at (tile/2, tile/2))
    # and show a 3x3-tile region around it to eyeball AA edges
    cx, cy = TILE // 2, TILE // 2
    crop_size = 3 * TILE
    sx, sy = max(0, cx - crop_size // 2), max(0, cy - crop_size // 2)
    ex, ey = min(w, sx + crop_size), min(h, sy + crop_size)

    b_crop = b[sy:ey, sx:ex].astype(np.uint8)
    g_crop = g[sy:ey, sx:ex].astype(np.uint8)
    d_crop = (diff[sy:ey, sx:ex].max(axis=2) * 4).clip(0, 255).astype(np.uint8)
    d_crop_rgb = np.stack([d_crop, d_crop, d_crop], axis=-1)

    # Stack horizontally with 2-px gap
    gap = np.zeros((b_crop.shape[0], 2, 3), dtype=np.uint8) + 128
    tile_row = np.concatenate([b_crop, gap, g_crop, gap, d_crop_rgb], axis=1)
    Image.fromarray(tile_row).resize(
        (tile_row.shape[1] * 4, tile_row.shape[0] * 4),
        Image.NEAREST,
    ).save(OUT_DIR / "dots-tile-crop.png")
    print(f"\nTile crop (canvas2d|golden|diff-x4): qa/captures/dots-tile-crop.png")

    # Full diff image (x4 amplified)
    full_diff = (max_chan * 4).clip(0, 255).astype(np.uint8)
    Image.fromarray(full_diff).save(OUT_DIR / "dots-diff.png")

    # SxS at native res
    gap_full = np.zeros((h, 4, 3), dtype=np.uint8) + 128
    sxs = np.concatenate([b.astype(np.uint8), gap_full, g.astype(np.uint8)], axis=1)
    Image.fromarray(sxs).save(OUT_DIR / "dots-sxs.png")

    # Save captures verbatim for the dispatch's "canvas2d-{fixture}.png + rust-{fixture}.png" ask
    Image.open(BROWSER).save(OUT_DIR / "dots-canvas2d.png")
    Image.open(GOLDEN).save(OUT_DIR / "dots-rust.png")

    summary = {
        "fixture": "parity_bg_pattern_dots",
        "density": 0.5,
        "tile_px": TILE,
        "radius_px": RADIUS,
        "image_size": [w, h],
        "max_delta_any_channel": int(max_chan.max()),
        "mean_delta_any_channel": float(max_chan.mean()),
        "pixels_over_10_pct": float(100*(max_chan>=10).mean()),
        "pixels_over_50_pct": float(100*(max_chan>=50).mean()),
        "pixels_over_100_pct": float(100*(max_chan>=100).mean()),
        "pixels_over_200_pct": float(100*(max_chan>=200).mean()),
        "histogram": [
            {"range": [int(edges[i]), int(edges[i+1])], "count": int(hist[i]),
             "pct": float(100*hist[i]/max_chan.size)}
            for i in range(len(hist))
        ],
    }
    (OUT_DIR / "dots-diag-summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nwrote {OUT_DIR / 'dots-diag-summary.json'}")


if __name__ == "__main__":
    main()
