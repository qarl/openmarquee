#!/usr/bin/env python3
"""Phase 3w: spatial diag for parity_bg_pattern_scanlines.

Hypothesis-confirmation probe: if Phase 3v's +1 px shift hypothesis
(pipeline-wide, not shader) is correct, scanlines should show a
+1 px shift on its Y axis (it's hard-step on Y only). If it does,
the cause is pipeline-side; if it doesn't, checker had something
checker-specific.

Canvas2D (ui/src/bg-system.js:330):
    for (let y = 0; y < height; y += tile)
        ctx.fillRect(0, y, width, 1);
Rust FS_PATTERN_SCANLINES uses floor(pos.y) + mod step.

At density=0.5 -> curved 0.25 -> tile = round(lerp(16, 2, 0.25))
= round(12.5) = 13.

Outputs:
  qa/captures/scanlines-{canvas2d,rust,diff,tile-crop}.png
  qa/captures/scanlines-diag-summary.json
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent.parent
BROWSER = REPO / "renderer" / "tests" / "parity" / "captures" / "parity_bg_pattern_scanlines.browser.png"
GOLDEN = REPO / "renderer" / "tests" / "golden" / "bg_pattern_scanlines.png"
OUT_DIR = REPO / "qa" / "captures"

TILE = 13


def main():
    b = np.asarray(Image.open(BROWSER).convert("RGB"), dtype=np.int16)
    g = np.asarray(Image.open(GOLDEN).convert("RGB"), dtype=np.int16)
    if b.shape != g.shape:
        b_r = Image.open(BROWSER).convert("RGB").resize((g.shape[1], g.shape[0]), Image.LANCZOS)
        b = np.asarray(b_r, dtype=np.int16)
    h, w, _ = g.shape

    diff = np.abs(b - g)
    max_chan = diff.max(axis=2)

    print(f"Image dims: {w} x {h}; assumed TILE={TILE}")
    print(f"Max delta any-channel:  {int(max_chan.max())}")
    print(f"Mean delta any-channel: {float(max_chan.mean()):.3f}")
    for t in (10, 50, 100, 200):
        n = int((max_chan >= t).sum())
        print(f"  delta>={t:3d}: {n:>8d} ({100*n/max_chan.size:.3f}%)")

    print("\nPer-quadrant mean:")
    for qy, qn in enumerate(("top", "bottom")):
        for qx, xn in enumerate(("left", "right")):
            y0, y1 = (0, h//2) if qy == 0 else (h//2, h)
            x0, x1 = (0, w//2) if qx == 0 else (w//2, w)
            print(f"  {qn:6s}-{xn:6s}: {max_chan[y0:y1, x0:x1].mean():.3f}")

    # Pixel-level Y-transition probe at column x=10:
    # First Y where browser changes color, vs first Y where golden changes.
    print("\nY-transition probe at column x=10:")
    prev_b = tuple(b[0, 10])
    prev_g = tuple(g[0, 10])
    found_b = found_g = False
    for y in range(1, min(h, 80)):
        cur_b = tuple(b[y, 10])
        cur_g = tuple(g[y, 10])
        if not found_b and cur_b != prev_b:
            print(f"  Canvas2D first Y-transition: y={y-1}->{y} ({prev_b[0]} -> {cur_b[0]})")
            found_b = True
        if not found_g and cur_g != prev_g:
            print(f"  Rust     first Y-transition: y={y-1}->{y} ({prev_g[0]} -> {cur_g[0]})")
            found_g = True
        prev_b, prev_g = cur_b, cur_g
        if found_b and found_g:
            break

    hist, edges = np.histogram(max_chan, bins=[0, 5, 10, 25, 50, 100, 150, 200, 256])
    print("\nHistogram:")
    for i in range(len(hist)):
        print(f"  [{edges[i]:3d}, {edges[i+1]:3d}): {hist[i]:>8d} ({100*hist[i]/max_chan.size:.3f}%)")

    # Crop showing ~5 scanlines (5*tile = 65 rows)
    crop_h = min(h, 5 * TILE + 4)
    crop_w = min(w, 120)
    b_c = b[:crop_h, :crop_w].astype(np.uint8)
    g_c = g[:crop_h, :crop_w].astype(np.uint8)
    d_c = (max_chan[:crop_h, :crop_w] * 4).clip(0, 255).astype(np.uint8)
    d_rgb = np.stack([d_c]*3, axis=-1)
    gap = np.full((b_c.shape[0], 2, 3), 128, dtype=np.uint8)
    strip = np.concatenate([b_c, gap, g_c, gap, d_rgb], axis=1)
    Image.fromarray(strip).resize((strip.shape[1]*4, strip.shape[0]*4), Image.NEAREST)\
        .save(OUT_DIR / "scanlines-tile-crop.png")

    Image.fromarray((max_chan*4).clip(0,255).astype(np.uint8)).save(OUT_DIR / "scanlines-diff.png")
    Image.open(BROWSER).save(OUT_DIR / "scanlines-canvas2d.png")
    Image.open(GOLDEN).save(OUT_DIR / "scanlines-rust.png")

    (OUT_DIR / "scanlines-diag-summary.json").write_text(json.dumps({
        "fixture": "parity_bg_pattern_scanlines",
        "tile_assumed": TILE, "image_size": [w, h],
        "max_delta": int(max_chan.max()),
        "mean_delta": float(max_chan.mean()),
        "pixels_over_50_pct": float(100*(max_chan>=50).mean()),
        "pixels_over_200_pct": float(100*(max_chan>=200).mean()),
    }, indent=2))


if __name__ == "__main__":
    main()
