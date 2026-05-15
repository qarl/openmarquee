#!/usr/bin/env python3
"""Phase 3e spot diagnostic on the 229/231 max_delta floor.

Phase 3c collapsed the mean-delta on most fixtures to <1, but every
fixture still has max_delta=229/231 = one pixel of full bg-vs-fg
disagreement. Phase 3d's lineHeight-tightening hypothesis was
refuted by data. Phase 3e localizes the actual cause via the same
playbook that worked for 1c (single-glyph) and 3b (capture-pipeline).

Pick font_inter -- single-line "INTER", WASM-path, yScale=1,
mean=0.50 (closest to clean parity by mean -- the few pixels
driving the 229 max are the entire residual signal).

Approach:
  1. Diff parity_font_inter.browser.png vs renderer/tests/golden/
     font_inter.png pixel-by-pixel.
  2. For every pixel with rgb-max-delta > 100, record:
     (x, y, canvas2d_RGB, rust_RGB, side_claiming_glyph)
  3. Spatial-cluster check: are the disagreement pixels at glyph
     leading/trailing edges? At a specific row? Random?
  4. Test the named hypotheses:
     (i) Math.round(drawX) integer-snap on JS vs Rust GL_LINEAR
         sub-pixel. Look for canvas-x at integer-pixel boundaries.
     (ii) Center-alignment fractional drawX: boxX + (boxW -
         targetW)/2. boxX = 0.05*1920 = 96. boxW = 0.9*1920 = 1728.
         targetW = result.width from WASM. drawX = 96 + (1728-W)/2.
         Fractional if W is odd, integer if W is even.
     (iii) box.x * canvas_width round-trip in float64. box.x = 0.05
          is NOT binary-representable; 0.05*1920 in Python = 96.0
          exactly, in JS = 96.0 exactly (verify).
     (iv) Combination.

Outputs (qa/captures/):
  floor-diag-font-inter.png   diff overlay (red on disagreement
                              pixels, original below)
  floor-diag-font-inter.json  pixel list + verdict
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
CANVAS_PNG = REPO / "renderer" / "tests" / "parity" / "captures" / "parity_font_inter.browser.png"
GOLDEN_PNG = REPO / "renderer" / "tests" / "golden" / "font_inter.png"
OUT_DIR = REPO / "qa" / "captures"
FIXTURE_JSON = REPO / "renderer" / "tests" / "fixtures" / "f0000000-0000-4000-8000-000000000015" / "item.json"


def main():
    from PIL import Image, ImageDraw
    import numpy as np

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"=== Phase 3e floor diagnostic on font_inter ===\n", file=sys.stderr)

    canvas = np.array(Image.open(CANVAS_PNG).convert("RGB"), dtype=np.int16)
    golden = np.array(Image.open(GOLDEN_PNG).convert("RGB"), dtype=np.int16)
    assert canvas.shape == golden.shape, f"shape mismatch {canvas.shape} vs {golden.shape}"
    h, w = canvas.shape[:2]
    print(f"image dims: {w}x{h}", file=sys.stderr)

    # Per-pixel max-delta across RGB.
    delta = np.abs(canvas - golden).max(axis=-1)
    max_d = int(delta.max())
    mean_d = float(delta.mean())
    print(f"max_delta:  {max_d}", file=sys.stderr)
    print(f"mean_delta: {mean_d:.3f}", file=sys.stderr)

    # Pixels with delta > 100. Those are the bg-vs-fg flip candidates.
    high_y, high_x = np.where(delta > 100)
    high_count = len(high_x)
    print(f"pixels with delta > 100:  {high_count}", file=sys.stderr)

    # Spatial bounding box of disagreement pixels.
    if high_count > 0:
        bb = {
            "x_min": int(high_x.min()),
            "x_max": int(high_x.max()),
            "y_min": int(high_y.min()),
            "y_max": int(high_y.max()),
            "y_unique": [int(y) for y in sorted(set(high_y.tolist()))],
        }
        print(f"  bounding box: x=[{bb['x_min']}..{bb['x_max']}], "
              f"y=[{bb['y_min']}..{bb['y_max']}]", file=sys.stderr)
        print(f"  rows touched: {len(bb['y_unique'])}  "
              f"(span {bb['y_max'] - bb['y_min'] + 1} rows)", file=sys.stderr)
    else:
        bb = {}

    # Per-pixel listing for the highest-delta pixels (cap at 50 for
    # legibility; if there are thousands, the bb + sample is enough).
    pixel_list = []
    if high_count > 0:
        # Sort by delta descending so the worst ones come first.
        deltas_at_high = delta[high_y, high_x]
        order = np.argsort(-deltas_at_high)
        for idx in order[:50]:
            x = int(high_x[idx])
            y = int(high_y[idx])
            c_rgb = canvas[y, x].tolist()
            r_rgb = golden[y, x].tolist()
            # Whose side claims "white-ish" (glyph) vs "dark-ish" (bg)?
            c_lum = 0.299*c_rgb[0] + 0.587*c_rgb[1] + 0.114*c_rgb[2]
            r_lum = 0.299*r_rgb[0] + 0.587*r_rgb[1] + 0.114*r_rgb[2]
            if c_lum > r_lum + 50:
                side = "canvas2d=glyph rust=bg"
            elif r_lum > c_lum + 50:
                side = "rust=glyph canvas2d=bg"
            else:
                side = "ambiguous"
            pixel_list.append({
                "x": x, "y": y,
                "canvas2d": c_rgb, "rust": r_rgb,
                "rgb_max_delta": int(delta[y, x]),
                "side": side,
            })

    # Box-coord computation (used by per-stem extraction below + the
    # hypothesis-test block further down).
    fixture = json.loads(FIXTURE_JSON.read_text())["item"]
    layer = fixture["text_layers"][0]
    box = layer["box"]
    text_align = layer["text_align"]
    box_x_canvas = box["x"] * 1920
    box_y_canvas = box["y"] * 1080
    box_w_canvas = box["w"] * 1920
    box_h_canvas = box["h"] * 1080
    if layer.get("font_size_pct"):
        pct = layer["font_size_pct"]
        size_px = round((box_w_canvas * pct) / 100)
    else:
        size_px = layer["font_size_px"]

    # ===== Per-stem extraction (Phase 3e review follow-up) =====
    # Subagent review flagged the centroid-based "42px shift" claim
    # as unsound (centroid of disagreement pixels != centroid of
    # glyphs). The cleaner measurement: find each glyph stem on
    # each side and measure their x positions directly.
    #
    # Method: for each column, count rows that are "glyph-ish"
    # (luminance > 128). A glyph STEM is a column where >half the
    # rows are glyph-ish (vertical stroke). Cluster contiguous
    # stem columns to find one position per glyph.
    def find_stem_centers(rgba_or_rgb):
        if rgba_or_rgb.ndim == 3 and rgba_or_rgb.shape[2] == 4:
            arr = rgba_or_rgb[..., :3]
        else:
            arr = rgba_or_rgb
        lum = (
            0.299*arr[..., 0] + 0.587*arr[..., 1] + 0.114*arr[..., 2]
        ).astype(np.float32)
        # Per-column count of glyph-ish rows.
        glyph_mask = lum > 128
        col_glyph_count = glyph_mask.sum(axis=0)
        # Stem columns: >50% glyph-ish in the box-y range.
        in_box = slice(int(box_y_canvas), int(box_y_canvas + box_h_canvas))
        col_in_box = glyph_mask[in_box, :].sum(axis=0)
        thresh = (int(box_h_canvas)) * 0.5
        stem_cols = np.where(col_in_box > thresh)[0]
        # Cluster contiguous runs into single stem positions
        # (center of each run).
        if len(stem_cols) == 0:
            return []
        runs = []
        cur_start = stem_cols[0]
        cur_end = stem_cols[0]
        for c in stem_cols[1:]:
            if c == cur_end + 1:
                cur_end = c
            else:
                runs.append((cur_start, cur_end))
                cur_start = cur_end = c
        runs.append((cur_start, cur_end))
        return [(int((s+e)/2), int(s), int(e)) for s, e in runs]

    canvas_stems = find_stem_centers(canvas)
    rust_stems = find_stem_centers(golden)
    print(f"\nPer-stem x analysis:", file=sys.stderr)
    print(f"  Canvas2D stems (center, x_start, x_end): {canvas_stems}",
          file=sys.stderr)
    print(f"  Rust     stems (center, x_start, x_end): {rust_stems}",
          file=sys.stderr)
    stem_deltas = []
    if len(canvas_stems) == len(rust_stems) and len(canvas_stems) > 0:
        print(f"  Per-stem delta (rust_center - canvas2d_center):",
              file=sys.stderr)
        for (cc, _, _), (rc, _, _) in zip(canvas_stems, rust_stems):
            delta_stem = rc - cc
            stem_deltas.append(delta_stem)
            print(f"    canvas2d_center={cc:>4d}  rust_center={rc:>4d}  "
                  f"delta=+{delta_stem}", file=sys.stderr)
    else:
        print(f"  WARN: stem count differs between sides; per-stem "
              f"alignment ambiguous.", file=sys.stderr)

    # ===== Hypothesis tests =====
    print(f"\nFixture math reconstruction:", file=sys.stderr)
    print(f"  box.x = {box['x']} -> canvas-X = {box_x_canvas!r}", file=sys.stderr)
    print(f"  box.y = {box['y']} -> canvas-Y = {box_y_canvas!r}", file=sys.stderr)
    print(f"  box.w = {box['w']} -> canvas-W = {box_w_canvas!r}", file=sys.stderr)
    print(f"  box.h = {box['h']} -> canvas-H = {box_h_canvas!r}", file=sys.stderr)
    print(f"  font_size_pct = {layer.get('font_size_pct')} -> "
          f"size_px = {size_px}", file=sys.stderr)
    print(f"  text_align = {text_align!r}", file=sys.stderr)

    binary_repr = {
        "box_x_canvas_is_integer": box_x_canvas == int(box_x_canvas),
        "box_y_canvas_is_integer": box_y_canvas == int(box_y_canvas),
        "box_w_canvas_is_integer": box_w_canvas == int(box_w_canvas),
        "box_h_canvas_is_integer": box_h_canvas == int(box_h_canvas),
    }
    print(f"\nFloat-precision check:", file=sys.stderr)
    for k, v in binary_repr.items():
        print(f"  {k}: {v}", file=sys.stderr)

    # ===== Visualization: red-overlay diff =====
    # Composite the canvas2d capture + red mask where delta > 100.
    overlay = Image.open(CANVAS_PNG).convert("RGB").copy()
    mask = np.zeros((h, w, 4), dtype=np.uint8)
    if high_count > 0:
        mask[high_y, high_x] = [255, 0, 0, 255]
    red_overlay = Image.fromarray(mask, mode="RGBA")
    overlay.paste(red_overlay, (0, 0), red_overlay.split()[3])
    overlay.save(OUT_DIR / "floor-diag-font-inter.png")

    # Aggregate direction split (review nit: was in doc, not in JSON).
    if high_count > 0:
        c_lum_all = (
            0.299*canvas[high_y, high_x, 0]
            + 0.587*canvas[high_y, high_x, 1]
            + 0.114*canvas[high_y, high_x, 2]
        )
        r_lum_all = (
            0.299*golden[high_y, high_x, 0]
            + 0.587*golden[high_y, high_x, 1]
            + 0.114*golden[high_y, high_x, 2]
        )
        direction_split = {
            "canvas2d_glyph_rust_bg": int((c_lum_all > r_lum_all + 50).sum()),
            "rust_glyph_canvas2d_bg": int((r_lum_all > c_lum_all + 50).sum()),
        }
    else:
        direction_split = {}

    out = {
        "fixture": "parity_font_inter",
        "image_dims": [w, h],
        "max_delta": max_d,
        "mean_delta": mean_d,
        "pixels_delta_gt_100": high_count,
        "disagreement_bbox": bb,
        "direction_split": direction_split,
        "canvas2d_stems": canvas_stems,
        "rust_stems": rust_stems,
        "per_stem_delta_rust_minus_canvas2d": stem_deltas,
        "top_50_pixels": pixel_list,
        "fixture_math": {
            "box_x_norm": box["x"],
            "box_y_norm": box["y"],
            "box_w_norm": box["w"],
            "box_h_norm": box["h"],
            "box_x_canvas_px": box_x_canvas,
            "box_y_canvas_px": box_y_canvas,
            "box_w_canvas_px": box_w_canvas,
            "box_h_canvas_px": box_h_canvas,
            "size_px": size_px,
            "text_align": text_align,
            "binary_repr_check": binary_repr,
        },
    }
    (OUT_DIR / "floor-diag-font-inter.json").write_text(json.dumps(out, indent=2))
    print(f"\n  floor-diag-font-inter.png: {OUT_DIR / 'floor-diag-font-inter.png'}",
          file=sys.stderr)
    print(f"  floor-diag-font-inter.json: {OUT_DIR / 'floor-diag-font-inter.json'}",
          file=sys.stderr)

    # ===== Top-5 disagreement pixels surfaced inline =====
    if pixel_list:
        print(f"\nTop-5 disagreement pixels:", file=sys.stderr)
        for p in pixel_list[:5]:
            print(f"  ({p['x']:4d}, {p['y']:4d})  canvas2d={p['canvas2d']}  "
                  f"rust={p['rust']}  delta={p['rgb_max_delta']}  {p['side']}",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
