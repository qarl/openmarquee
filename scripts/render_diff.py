#!/usr/bin/env python3
"""Pixel-percentage diff for golden-master render tests.

Compares two PNGs and reports the fraction of pixels whose L1
distance exceeds a per-channel tolerance. Used by render_tests.sh
to validate that the renderer produces visually-equivalent output
to a checked-in golden.

Tolerance design:
  * Per-channel L1: |new[c] - golden[c]| <= tolerance for each
    of R/G/B. Tolerates anti-aliasing jitter at glyph edges and
    minor floating-point drift between vc4 driver versions.
  * "Different" pixel = ANY channel exceeds tolerance.
  * Failure threshold = fraction of different pixels / total.
    Set high enough to permit AA fringing along glyph outlines
    (a 1080p text-heavy slide has roughly 1-3% of pixels in AA
    boundary regions).

Default thresholds (tolerance=10/255, threshold=0.5%) catch
structural changes (wrong shader, wrong blend mode, wrong layout)
without false-positiving on AA noise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("FAIL: PIL/Pillow not installed (pip3 install Pillow)", file=sys.stderr)
    sys.exit(2)


def diff_pngs(
    new_path: Path,
    golden_path: Path,
    tolerance: int = 10,
    threshold: float = 0.005,
) -> tuple[bool, str]:
    """Returns (is_pass, summary_text)."""
    if not golden_path.exists():
        return (False, f"golden missing: {golden_path}")
    if not new_path.exists():
        return (False, f"capture missing: {new_path}")
    new_img = Image.open(new_path).convert("RGB")
    gold_img = Image.open(golden_path).convert("RGB")
    if new_img.size != gold_img.size:
        return (
            False,
            f"size mismatch: new={new_img.size} golden={gold_img.size}",
        )
    new_data = new_img.tobytes()
    gold_data = gold_img.tobytes()
    total_px = new_img.size[0] * new_img.size[1]
    diff_px = 0
    max_delta = 0
    # Walk RGB triplets.
    for i in range(0, len(new_data), 3):
        dr = abs(new_data[i] - gold_data[i])
        dg = abs(new_data[i + 1] - gold_data[i + 1])
        db = abs(new_data[i + 2] - gold_data[i + 2])
        max_delta = max(max_delta, dr, dg, db)
        if dr > tolerance or dg > tolerance or db > tolerance:
            diff_px += 1
    fraction = diff_px / total_px
    is_pass = fraction <= threshold
    summary = (
        f"{new_img.size[0]}x{new_img.size[1]} pixels, "
        f"{diff_px} differ (>{tolerance}/255 in any channel) = "
        f"{fraction * 100:.3f}% [threshold {threshold * 100:.2f}%], "
        f"max_delta={max_delta}"
    )
    return (is_pass, summary)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("new_png", type=Path, help="freshly captured PNG")
    ap.add_argument("golden_png", type=Path, help="checked-in golden PNG")
    ap.add_argument(
        "--tolerance",
        type=int,
        default=10,
        help="per-channel L1 tolerance 0..255 (default 10)",
    )
    ap.add_argument(
        "--threshold",
        type=float,
        default=0.005,
        help="fraction of different pixels permitted (default 0.005 = 0.5%%)",
    )
    args = ap.parse_args()
    is_pass, summary = diff_pngs(
        args.new_png, args.golden_png, args.tolerance, args.threshold
    )
    print(f"{'PASS' if is_pass else 'FAIL'}: {summary}")
    return 0 if is_pass else 1


if __name__ == "__main__":
    sys.exit(main())
