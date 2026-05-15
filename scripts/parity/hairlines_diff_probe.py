#!/usr/bin/env python3
"""Phase 3m hairlines disambiguation: emit diff visualizations for the
3 closest-to-PASS hairlines-tier fixtures.

Purpose: if the loud pixels (delta>=50) for bg_pattern_gradient (max 197),
bg_pattern_solid (max 221), and font_inter (max 231) cluster in the
SAME canvas regions, Cause B is one mechanism. If they cluster in
DIFFERENT regions, Cause B has 2-3 sub-causes worth attacking
separately.

Outputs (under qa/captures/):
  hairlines-diff-<fixture>.png       per-pixel max-channel delta (grayscale, 4x amplified)
  hairlines-loud-<fixture>.png       binary loud-pixel mask (red where any-chan delta>=50)
  hairlines-overlay-<fixture>.png    golden + red-tinted loud-pixel mask
  hairlines-diff-summary.json        per-fixture loud-pixel bounding box + count
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

REPO = Path(__file__).resolve().parent.parent.parent
CAP_DIR = REPO / "renderer" / "tests" / "parity" / "captures"
GOLDEN_DIR = REPO / "renderer" / "tests" / "golden"
OUT_DIR = REPO / "qa" / "captures"

# Closest-to-PASS hairlines fixtures from Phase 3l-post classification.
# All three have mean_delta < 1.0 (diff is concentrated, not scattered).
TARGETS = [
    ("parity_bg_pattern_gradient", "bg_pattern_gradient", 197),
    ("parity_bg_pattern_solid",    "bg_pattern_solid",    221),
    ("parity_font_inter",          "font_inter",          231),
]
LOUD_THRESHOLD = 50  # gate floor; matches parity_tests.sh max_delta<=50


def load_pair(name: str, golden_name: str):
    browser = Image.open(CAP_DIR / f"{name}.browser.png").convert("RGB")
    golden = Image.open(GOLDEN_DIR / f"{golden_name}.png").convert("RGB")
    if browser.size != golden.size:
        browser = browser.resize(golden.size, Image.LANCZOS)
    return browser, golden


def per_pixel_max_delta(a: Image.Image, b: Image.Image) -> Image.Image:
    # ImageChops.difference -> abs delta per channel. Take per-pixel
    # max across channels via convert('L') after splitting.
    diff = ImageChops.difference(a, b)
    r, g, bl = diff.split()
    # max(r, g, b) per pixel
    rg = ImageChops.lighter(r, g)
    return ImageChops.lighter(rg, bl)


def loud_mask(diff_l: Image.Image, threshold: int) -> Image.Image:
    return diff_l.point(lambda v: 255 if v >= threshold else 0, mode="L")


def overlay(base: Image.Image, mask: Image.Image, tint=(255, 0, 0, 180)) -> Image.Image:
    out = base.convert("RGBA")
    red = Image.new("RGBA", base.size, tint)
    out = Image.composite(red, out, mask)
    return out.convert("RGB")


def bbox_of_mask(mask: Image.Image):
    bb = mask.getbbox()
    if bb is None:
        return None
    return {"left": bb[0], "top": bb[1], "right": bb[2], "bottom": bb[3]}


def count_nonzero(mask: Image.Image) -> int:
    return sum(1 for v in mask.getdata() if v > 0)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = {"threshold": LOUD_THRESHOLD, "fixtures": []}

    for name, golden_name, max_delta in TARGETS:
        browser, golden = load_pair(name, golden_name)
        diff_l = per_pixel_max_delta(browser, golden)
        mask = loud_mask(diff_l, LOUD_THRESHOLD)

        # Amplified grayscale diff for eyeballing low deltas
        amp = diff_l.point(lambda v: min(255, v * 4))

        amp.save(OUT_DIR / f"hairlines-diff-{golden_name}.png")
        mask.save(OUT_DIR / f"hairlines-loud-{golden_name}.png")
        overlay(golden, mask).save(OUT_DIR / f"hairlines-overlay-{golden_name}.png")

        bb = bbox_of_mask(mask)
        n_loud = count_nonzero(mask)
        w, h = golden.size
        summary["fixtures"].append({
            "name": name,
            "size": [w, h],
            "max_delta_reported": max_delta,
            "loud_pixel_count": n_loud,
            "loud_pixel_pct": round(100.0 * n_loud / (w * h), 4),
            "loud_pixel_bbox": bb,
        })
        print(f"{name:32s}  loud={n_loud:>6d} pixels ({100.0*n_loud/(w*h):.4f}%)  bbox={bb}")

    (OUT_DIR / "hairlines-diff-summary.json").write_text(
        json.dumps(summary, indent=2)
    )
    print(f"\nwrote {OUT_DIR / 'hairlines-diff-summary.json'}")


if __name__ == "__main__":
    main()
