#!/usr/bin/env python3
"""Phase 3t: spatial diag for parity_bg_pattern_halftone.

Hypothesis from code-reading (same as Phase 3s dots):
- Canvas2D (ui/src/bg-system.js:286-306) uses ctx.arc + ctx.fill per
  layer (two offset grids), browser bilinear AA at each circle.
- Rust FS_PATTERN_HALFTONE uses step(d_min2, r2) -- HARD step on the
  min-distance to either grid's nearest center.

Predicted: same smoothstep AA fix that Phase 3s applied to dots.

Outputs:
  qa/captures/halftone-canvas2d.png  (Canvas2D capture)
  qa/captures/halftone-rust.png      (Rust golden)
  qa/captures/halftone-diff.png      (per-pixel max-channel delta, x4)
  qa/captures/halftone-tile-crop.png (3-tile crop showing AA ring)
  qa/captures/halftone-diag-summary.json
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent.parent
BROWSER = REPO / "renderer" / "tests" / "parity" / "captures" / "parity_bg_pattern_halftone.browser.png"
GOLDEN = REPO / "renderer" / "tests" / "golden" / "bg_pattern_halftone.png"
OUT_DIR = REPO / "qa" / "captures"

# halftone fixture density=0.5 -> tile = round(lerp(60, 6, 0.5)) = 33;
# radius = round(33 * 0.34) = 11. Two layers, layer 1 offset by
# tile//2 = 16 in both axes.
TILE = 33
RADIUS = 11


def main():
    b = np.asarray(Image.open(BROWSER).convert("RGB"), dtype=np.int16)
    g = np.asarray(Image.open(GOLDEN).convert("RGB"), dtype=np.int16)
    if b.shape != g.shape:
        b_r = Image.open(BROWSER).convert("RGB").resize((g.shape[1], g.shape[0]), Image.LANCZOS)
        b = np.asarray(b_r, dtype=np.int16)
    h, w, _ = g.shape

    diff = np.abs(b - g)
    max_chan = diff.max(axis=2)

    print(f"Image dims: {w} x {h}")
    print(f"Max delta any-channel:  {int(max_chan.max())}")
    print(f"Mean delta any-channel: {float(max_chan.mean()):.3f}")
    for thresh in (10, 50, 100, 200):
        n = int((max_chan >= thresh).sum())
        print(f"Pixels with delta>={thresh:3d}: {n:>8d}  ({100*n/max_chan.size:.3f}%)")

    for ci, cname in enumerate("RGB"):
        print(f"  {cname}: max={int(diff[..., ci].max())} mean={diff[..., ci].mean():.3f}")

    # Per-quadrant
    print("\nPer-quadrant mean:")
    for qy, qy_n in enumerate(("top", "bottom")):
        for qx, qx_n in enumerate(("left", "right")):
            y0, y1 = (0, h//2) if qy == 0 else (h//2, h)
            x0, x1 = (0, w//2) if qx == 0 else (w//2, w)
            print(f"  {qy_n:6s}-{qx_n:6s}: mean={max_chan[y0:y1, x0:x1].mean():.3f}")

    # Histogram
    hist, edges = np.histogram(max_chan, bins=[0, 5, 10, 25, 50, 100, 150, 200, 256])
    print("\nHistogram:")
    for i in range(len(hist)):
        pct = 100 * hist[i] / max_chan.size
        print(f"  [{edges[i]:3d}, {edges[i+1]:3d}): {hist[i]:>8d}  ({pct:.3f}%)")

    # 3-tile crop around first layer-1 dot at (tile/2, tile/2) ≈ (16, 16)
    crop_size = 3 * TILE
    sy, sx = 0, 0
    ey, ex = min(h, crop_size), min(w, crop_size)
    b_crop = b[sy:ey, sx:ex].astype(np.uint8)
    g_crop = g[sy:ey, sx:ex].astype(np.uint8)
    d_crop = (max_chan[sy:ey, sx:ex] * 4).clip(0, 255).astype(np.uint8)
    d_crop_rgb = np.stack([d_crop]*3, axis=-1)
    gap = np.full((b_crop.shape[0], 2, 3), 128, dtype=np.uint8)
    strip = np.concatenate([b_crop, gap, g_crop, gap, d_crop_rgb], axis=1)
    Image.fromarray(strip).resize(
        (strip.shape[1]*4, strip.shape[0]*4), Image.NEAREST,
    ).save(OUT_DIR / "halftone-tile-crop.png")

    # Full diff (amplified)
    Image.fromarray((max_chan*4).clip(0, 255).astype(np.uint8)).save(OUT_DIR / "halftone-diff.png")
    Image.open(BROWSER).save(OUT_DIR / "halftone-canvas2d.png")
    Image.open(GOLDEN).save(OUT_DIR / "halftone-rust.png")

    summary = {
        "fixture": "parity_bg_pattern_halftone",
        "tile_px": TILE,
        "radius_px": RADIUS,
        "image_size": [w, h],
        "max_delta_any_channel": int(max_chan.max()),
        "mean_delta_any_channel": float(max_chan.mean()),
        "pixels_over_10_pct": float(100*(max_chan>=10).mean()),
        "pixels_over_50_pct": float(100*(max_chan>=50).mean()),
        "pixels_over_100_pct": float(100*(max_chan>=100).mean()),
        "pixels_over_200_pct": float(100*(max_chan>=200).mean()),
    }
    (OUT_DIR / "halftone-diag-summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nwrote {OUT_DIR / 'halftone-diag-summary.json'}")


if __name__ == "__main__":
    main()
