#!/usr/bin/env python3
"""Phase 3v: spatial diag for parity_bg_pattern_checker.

Both renderers use pixel-aligned hard-step at integer tile
boundaries:
- Canvas2D (ui/src/bg-system.js:335-347): ctx.fillRect at
  (col*tile, row*tile, tile, tile). No AA.
- Rust FS_PATTERN_CHECKER (hdmi_logic.rs:2237): floor(pos/tile)
  + mod(gx+gy, 2). Hard step.

Predicted divergences:
- Cause B: "CHECKER" text glyph AA (architectural floor).
- Possibly a y-flip or tile-anchor offset.

Outputs:
  qa/captures/checker-{canvas2d,rust,diff,tile-crop}.png
  qa/captures/checker-diag-summary.json
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent.parent
BROWSER = REPO / "renderer" / "tests" / "parity" / "captures" / "parity_bg_pattern_checker.browser.png"
GOLDEN = REPO / "renderer" / "tests" / "golden" / "bg_pattern_checker.png"
OUT_DIR = REPO / "qa" / "captures"

# density=0.5 -> curved 0.25 -> tile = round(lerp(60, 4, 0.25))
# = round(46) = 46 (even).
TILE = 46


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
    for ci, cn in enumerate("RGB"):
        print(f"  {cn}: max={int(diff[..., ci].max())} mean={diff[..., ci].mean():.3f}")

    print("\nPer-quadrant mean:")
    for qy, qn in enumerate(("top", "bottom")):
        for qx, xn in enumerate(("left", "right")):
            y0, y1 = (0, h//2) if qy == 0 else (h//2, h)
            x0, x1 = (0, w//2) if qx == 0 else (w//2, w)
            print(f"  {qn:6s}-{xn:6s}: {max_chan[y0:y1, x0:x1].mean():.3f}")

    # Mortar-line probe: rows where (y % TILE == 0) should match.
    print("\nRow-mean delta along expected tile boundaries (every TILE):")
    for y in range(0, min(h, 10*TILE), TILE):
        row_mean = max_chan[y:y+TILE, :].mean() if y+TILE <= h else max_chan[y:, :].mean()
        print(f"  rows[{y:4d}, {y+TILE:4d}): mean={row_mean:.3f}")

    hist, edges = np.histogram(max_chan, bins=[0, 5, 10, 25, 50, 100, 150, 200, 256])
    print("\nHistogram:")
    for i in range(len(hist)):
        print(f"  [{edges[i]:3d}, {edges[i+1]:3d}): {hist[i]:>8d} ({100*hist[i]/max_chan.size:.3f}%)")

    crop = min(h, 3 * TILE + 4), min(w, 3 * TILE + 4)
    b_c = b[:crop[0], :crop[1]].astype(np.uint8)
    g_c = g[:crop[0], :crop[1]].astype(np.uint8)
    d_c = (max_chan[:crop[0], :crop[1]] * 4).clip(0, 255).astype(np.uint8)
    d_rgb = np.stack([d_c]*3, axis=-1)
    gap = np.full((b_c.shape[0], 2, 3), 128, dtype=np.uint8)
    strip = np.concatenate([b_c, gap, g_c, gap, d_rgb], axis=1)
    Image.fromarray(strip).resize((strip.shape[1]*3, strip.shape[0]*3), Image.NEAREST)\
        .save(OUT_DIR / "checker-tile-crop.png")

    Image.fromarray((max_chan*4).clip(0,255).astype(np.uint8)).save(OUT_DIR / "checker-diff.png")
    Image.open(BROWSER).save(OUT_DIR / "checker-canvas2d.png")
    Image.open(GOLDEN).save(OUT_DIR / "checker-rust.png")

    (OUT_DIR / "checker-diag-summary.json").write_text(json.dumps({
        "fixture": "parity_bg_pattern_checker",
        "tile_assumed": TILE, "image_size": [w, h],
        "max_delta": int(max_chan.max()),
        "mean_delta": float(max_chan.mean()),
        "pixels_over_50_pct": float(100*(max_chan>=50).mean()),
        "pixels_over_200_pct": float(100*(max_chan>=200).mean()),
    }, indent=2))


if __name__ == "__main__":
    main()
