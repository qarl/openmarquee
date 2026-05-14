#!/usr/bin/env python3
"""SSIM + L1 stats between SB and fullres transition-mid captures.

QA-direct (2026-05-13) Atlas SB visual sanity harness. Compares each
transition's half-res Atlas SB output (`--capture-sb-mid`) against the
full-res reference baseline (`--capture-fullres-mid`) for the same
(from, to, kind, t) inputs. The two PNGs come from the SAME composite
shader; the only difference is bake resolution. SSIM ≥ 0.95 is the
spec §11 / Atlas SB acceptance gate.

Usage:
  python3 scripts/atlas_sb_ssim.py [--dir DIR]

Inputs (default --dir /tmp/atlas-sb-viz/):
  transition_mid_{KIND}_sb.png
  transition_mid_{KIND}_fullres.png
for KIND in {cut, fade, wipe, slide, pixelate}.

If --include-stretch, also diffs the motion-on-both-sides stretch
fixture (transition_mid_stretch_motion_{sb,fullres}.png).

Output: markdown table to stdout. Exit code 0 if all kinds pass the
gate, 1 if any fail.

Generation: see qa/atlas-sb-visual-sanity-2026-05-13.md for the Pi-
side commands that produce the source PNGs. Re-running on a different
Pi or driver version requires re-bless; SSIM here measures intra-Pi
SB-vs-fullres drift, not bit-identical cross-Pi reproducibility.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import numpy as np
    from PIL import Image
    from skimage.metrics import structural_similarity as ssim_fn
except ImportError as e:
    sys.exit(f"need numpy + Pillow + scikit-image: {e}")

KINDS = ["cut", "fade", "wipe", "slide", "pixelate"]
GATE = 0.95  # spec §11 / Atlas SB acceptance


def load(p: Path):
    img = Image.open(p).convert("RGB")
    return np.asarray(img, dtype=np.int16)


def stats(sb, fr):
    diff = np.abs(sb - fr).astype(np.uint16)
    px_diff = diff.max(axis=2)
    max_delta = int(px_diff.max())
    mean_delta = float(px_diff.mean())
    pct_over_50 = float((px_diff > 50).sum()) / px_diff.size * 100.0
    sb_g = sb.astype(np.float32).mean(axis=2)
    fr_g = fr.astype(np.float32).mean(axis=2)
    ssim = float(ssim_fn(sb_g, fr_g, data_range=255.0))
    return ssim, max_delta, mean_delta, pct_over_50


def diff_pair(root: Path, name: str) -> tuple[float, int, float, float] | None:
    sb_p = root / f"transition_mid_{name}_sb.png"
    fr_p = root / f"transition_mid_{name}_fullres.png"
    if not sb_p.exists() or not fr_p.exists():
        return None
    sb = load(sb_p)
    fr = load(fr_p)
    if sb.shape != fr.shape:
        sys.exit(f"shape mismatch for {name}: {sb.shape} vs {fr.shape}")
    return stats(sb, fr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="/tmp/atlas-sb-viz",
                    help="directory holding the captured PNGs")
    ap.add_argument("--include-stretch", action="store_true",
                    help="also diff the motion-on-both-sides stretch fixture")
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        sys.exit(f"capture dir not found: {root}")

    print()
    print("| Kind     | SSIM   | Max ΔL1 | Mean ΔL1 | %px Δ>50 | Gate (≥0.95) |")
    print("|----------|--------|---------|----------|----------|--------------|")
    fail = []
    last_shape = None
    for k in KINDS:
        r = diff_pair(root, k)
        if r is None:
            print(f"| {k:<8} | MISSING |  -      |  -       |  -       | -            |")
            continue
        ssim, mx, mn, pct = r
        verdict = "PASS" if ssim >= GATE else "FAIL"
        if verdict == "FAIL":
            fail.append((k, ssim))
        print(f"| {k:<8} | {ssim:.4f} | {mx:>7d} | {mn:>8.3f} | {pct:>7.3f}% | {verdict:<12} |")

    if args.include_stretch:
        r = diff_pair(root, "stretch_motion")
        if r is None:
            print(f"| stretch  | MISSING |  -      |  -       |  -       | -            |")
        else:
            ssim, mx, mn, pct = r
            verdict = "PASS" if ssim >= GATE else "FAIL"
            if verdict == "FAIL":
                fail.append(("stretch_motion", ssim))
            print(f"| stretch  | {ssim:.4f} | {mx:>7d} | {mn:>8.3f} | {pct:>7.3f}% | {verdict:<12} |")

    print()
    print(f"gate: SSIM ≥ {GATE}")
    if fail:
        print(f"FAILURES: {fail}")
        sys.exit(1)
    print("All checked fixtures PASS at the 0.95 gate.")


if __name__ == "__main__":
    main()
