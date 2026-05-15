#!/usr/bin/env python3
"""Phase 3p: spatial localization of Cause B (parity_font_inter).

Phase 3n + 3o triangulated Cause B to the upload-stage (texture
sampling / drawImage AA / quad placement / DPR). This probe
disambiguates among 4 ranked candidates from ee08d1b:

  1. Quad placement / scaling rounding
     -> loud pixels concentrate at the quad rectangle PERIMETER
        (4 thin lines along the outer edges of the placed text box)
  2. Texture filter mode (GL_LINEAR vs browser bilinear)
     -> loud pixels form a uniform 1-2 px AA-ring parallel to every
        glyph outline (not just box edges)
  3. DPR mismatch (Playwright DPR=1 vs Retina DPR=2)
     -> loud pixels translate by exactly N pixels in one direction
        (1 px @ DPR=1 mismatch, 2 px @ Retina)
  4. WASM 12-byte header parse off-by-N in ui/src/rasterize.js
     -> loud pixels shift the entire glyph by exactly N pixels
        (uniform translation)

Method:
- Load browser capture + golden, compute per-pixel max-channel
  abs delta, threshold at >=50 (gate floor).
- Compute the predicted quad rectangle perimeter from
  box_to_ndc_quad math + fixture box (parity_font_inter:
  x=0.05, y=0.4, w=0.9, h=0.2 on 1920x1080; bitmap dims from
  Phase 3i = 3020x757; scale-down-only halign-center valign-middle).
- Compute glyph outline pixels from the golden via Sobel edge
  detection (gradient magnitude > threshold).
- Measure:
  * Fraction of loud pixels within 2 px of the QUAD PERIMETER
  * Fraction of loud pixels within 2 px of any GLYPH OUTLINE
  * Best translation offset (dx, dy) maximizing IoU between
    loud-pixel mask and outline mask -- if (0, 0) wins, no
    translation; if non-zero, candidate (3) or (4).

Outputs:
  qa/captures/cause-b-localization-font-inter.png       diagnostic overlay
  qa/captures/cause-b-localization-summary-2026-05-15.json   metrics

Run:
  python3 scripts/parity/cause_b_localize.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent.parent
BROWSER = REPO / "renderer" / "tests" / "parity" / "captures" / "parity_font_inter.browser.png"
GOLDEN = REPO / "renderer" / "tests" / "golden" / "font_inter.png"
OUT_OVERLAY = REPO / "qa" / "captures" / "cause-b-localization-font-inter.png"
OUT_JSON = REPO / "qa" / "captures" / "cause-b-localization-summary-2026-05-15.json"

LOUD_THRESHOLD = 50
EDGE_THRESHOLD = 60  # Sobel grad magnitude threshold for glyph outlines
NEAR_PX = 2          # "within N pixels" radius for proximity metrics

# parity_font_inter quad math: replicate hdmi_logic::box_to_ndc_quad
# scale-down-only + halign=center + valign=middle. Phase 3j made the
# scale factor pad-aware (bm_pad=1); the Rust value at HEAD passes
# pad=1 which means the effective ink dims are (bm_w - 2, bm_h - 2)
# but placed_w/h still use bm_w_f * scale.
FIXTURE = {
    "box_x": 0.05, "box_y": 0.4, "box_w": 0.9, "box_h": 0.2,
    "mode_w": 1920, "mode_h": 1080,
    "bm_w": 3020, "bm_h": 757,   # from Phase 3i advance probe ("INTER" at 1037)
    "bm_pad": 1,
}


def compute_quad_rect():
    f = FIXTURE
    box_left = f["box_x"] * f["mode_w"]
    box_top = f["box_y"] * f["mode_h"]
    box_w_px = max(f["box_w"] * f["mode_w"], 1.0)
    box_h_px = max(f["box_h"] * f["mode_h"], 1.0)
    bm_w_f = float(f["bm_w"])
    bm_h_f = float(f["bm_h"])
    pad2 = 2.0 * f["bm_pad"]
    ink_w = max(bm_w_f - pad2, 1.0)
    ink_h = max(bm_h_f - pad2, 1.0)
    # Phase 3j: scale uses INK dims, not bitmap dims.
    s_w = (box_w_px / ink_w) if ink_w > box_w_px else 1.0
    s_h = (box_h_px / ink_h) if ink_h > box_h_px else 1.0
    scale = min(s_w, s_h)
    placed_w = bm_w_f * scale
    placed_h = bm_h_f * scale
    dst_left = box_left + (box_w_px - placed_w) * 0.5
    dst_top = box_top + (box_h_px - placed_h) * 0.5
    dst_right = dst_left + placed_w
    dst_bottom = dst_top + placed_h
    return dst_left, dst_top, dst_right, dst_bottom


def load_pair():
    b = Image.open(BROWSER).convert("RGB")
    g = Image.open(GOLDEN).convert("RGB")
    if b.size != g.size:
        b = b.resize(g.size, Image.LANCZOS)
    return np.asarray(b, dtype=np.int16), np.asarray(g, dtype=np.int16)


def loud_mask(b: np.ndarray, g: np.ndarray) -> np.ndarray:
    d = np.abs(b - g).max(axis=2)
    return (d >= LOUD_THRESHOLD)


def glyph_outline_mask(g: np.ndarray) -> np.ndarray:
    """Sobel gradient magnitude on golden grayscale > threshold."""
    gray = g.mean(axis=2).astype(np.float32)
    # 3x3 Sobel kernels
    kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=np.float32)
    ky = kx.T
    def conv2d(a, k):
        from numpy.lib.stride_tricks import sliding_window_view
        win = sliding_window_view(a, k.shape)
        return (win * k).sum(axis=(-2, -1))
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[1:-1, 1:-1] = conv2d(gray, kx)
    gy[1:-1, 1:-1] = conv2d(gray, ky)
    mag = np.hypot(gx, gy)
    return mag >= EDGE_THRESHOLD


def quad_perimeter_mask(shape, rect, thickness=1) -> np.ndarray:
    h, w = shape
    mask = np.zeros((h, w), dtype=bool)
    l, t, r, b = [int(round(v)) for v in rect]
    l = max(0, min(w - 1, l))
    r = max(0, min(w - 1, r))
    t = max(0, min(h - 1, t))
    b = max(0, min(h - 1, b))
    th = thickness
    # 4 edges, "th" thick on the inside
    mask[t:t+th, l:r+1] = True       # top
    mask[b-th+1:b+1, l:r+1] = True   # bottom
    mask[t:b+1, l:l+th] = True       # left
    mask[t:b+1, r-th+1:r+1] = True   # right
    return mask


def dilate_mask(m: np.ndarray, radius: int) -> np.ndarray:
    """Manhattan-radius dilation via repeated 4-shift unions."""
    out = m.copy()
    for _ in range(radius):
        shifted = out.copy()
        shifted[1:, :] |= out[:-1, :]
        shifted[:-1, :] |= out[1:, :]
        shifted[:, 1:] |= out[:, :-1]
        shifted[:, :-1] |= out[:, 1:]
        out = shifted
    return out


def fraction_within(loud: np.ndarray, target: np.ndarray, radius: int) -> float:
    t_dilated = dilate_mask(target, radius)
    inter = loud & t_dilated
    n_loud = int(loud.sum())
    if n_loud == 0:
        return 0.0
    return float(inter.sum()) / n_loud


def best_translation(loud: np.ndarray, outline: np.ndarray,
                      max_shift: int = 4) -> tuple[int, int, float]:
    """Find (dx, dy) maximizing intersection of loud with shifted outline.
    Ties go to whichever offset is visited first (range starts at
    -max_shift). That can report e.g. (1, -1) when (0, 0) ties — harmless,
    because the caller's translation verdict requires `trans_score > base_score`
    (strict), where base_score = score at offset (0, 0)."""
    best = (0, 0, -1)
    for dy in range(-max_shift, max_shift + 1):
        for dx in range(-max_shift, max_shift + 1):
            shifted = np.roll(outline, shift=(dy, dx), axis=(0, 1))
            score = int((loud & shifted).sum())
            if score > best[2]:
                best = (dx, dy, score)
    return best


def make_overlay(g: np.ndarray, loud: np.ndarray, quad_rect, outline: np.ndarray) -> Image.Image:
    """Render golden + colored overlays:
       - red 50% where loud-pixel mask is set
       - cyan 1-px outline of the quad rectangle
    """
    h, w, _ = g.shape
    base = Image.fromarray(g.astype(np.uint8), "RGB").convert("RGBA")
    # Red loud overlay
    overlay = np.zeros((h, w, 4), dtype=np.uint8)
    overlay[loud] = [255, 0, 0, 160]
    overlay_img = Image.fromarray(overlay, "RGBA")
    out = Image.alpha_composite(base, overlay_img)
    # Cyan quad rectangle outline
    draw = ImageDraw.Draw(out)
    l, t, r, b = [int(round(v)) for v in quad_rect]
    draw.rectangle([l, t, r, b], outline=(0, 255, 255, 255), width=2)
    return out.convert("RGB")


def main():
    print(f"Loading browser: {BROWSER}")
    print(f"Loading golden:  {GOLDEN}")
    b, g = load_pair()
    h, w, _ = g.shape
    print(f"Image dims: {w}x{h}")

    quad = compute_quad_rect()
    print(f"Predicted quad rect (l,t,r,b): "
          f"({quad[0]:.2f}, {quad[1]:.2f}, {quad[2]:.2f}, {quad[3]:.2f})")

    loud = loud_mask(b, g)
    outline = glyph_outline_mask(g)
    perimeter = quad_perimeter_mask((h, w), quad, thickness=1)

    n_loud = int(loud.sum())
    n_outline = int(outline.sum())
    n_perimeter = int(perimeter.sum())
    print(f"Loud pixels (>=50): {n_loud} ({100.0*n_loud/(h*w):.4f}%)")
    print(f"Outline pixels: {n_outline}")
    print(f"Perimeter pixels: {n_perimeter}")

    frac_loud_near_outline = fraction_within(loud, outline, NEAR_PX)
    frac_loud_near_perimeter = fraction_within(loud, perimeter, NEAR_PX)
    print(f"Loud pixels within {NEAR_PX}px of GLYPH OUTLINE:    {100*frac_loud_near_outline:.2f}%")
    print(f"Loud pixels within {NEAR_PX}px of QUAD PERIMETER:    {100*frac_loud_near_perimeter:.2f}%")

    dx, dy, trans_score = best_translation(loud, outline, max_shift=4)
    base_score = int((loud & outline).sum())
    print(f"Outline match: at offset (0,0): {base_score}, "
          f"best at ({dx},{dy}): {trans_score} "
          f"(gain {trans_score - base_score})")

    # Verdict heuristics. Decisive metric: base_score (loud ∩ outline at
    # offset 0,0) / n_loud. Translation only counts if it yields a
    # POSITIVE gain over the offset-0 baseline -- a tie is not a shift.
    verdict = None
    reason = []
    base_ratio = (base_score / n_loud) if n_loud > 0 else 0.0
    has_translation = (trans_score > base_score) and (dx != 0 or dy != 0)
    if frac_loud_near_outline >= 0.80:
        if has_translation:
            if abs(dx) >= 1 and dy == 0:
                verdict = "candidate_4_or_3_horizontal_translation"
                reason.append(
                    f"loud pixels shift by ({dx},0) -- horizontal-only suggests "
                    f"header-parse or DPR-x"
                )
            else:
                verdict = "candidate_3_dpr_or_translation"
                reason.append(
                    f"loud pixels shift by ({dx},{dy}); gain "
                    f"{trans_score - base_score} over offset-0"
                )
        else:
            verdict = "candidate_2_texture_filter_aa"
            reason.append(
                f"{100*frac_loud_near_outline:.0f}% of loud pixels within "
                f"{NEAR_PX}px of glyph outlines; {100*base_ratio:.0f}% sit "
                f"EXACTLY on outline pixels (no dilation); no translation "
                f"(best offset gain = {trans_score - base_score})"
            )
    elif frac_loud_near_perimeter >= 0.40:
        verdict = "candidate_1_quad_perimeter"
        reason.append(
            f"{100*frac_loud_near_perimeter:.0f}% of loud pixels within "
            f"{NEAR_PX}px of quad perimeter"
        )
    else:
        verdict = "mixed_or_unknown"
        reason.append("no candidate dominates: surface for follow-up")
    print(f"\nVERDICT: {verdict}")
    print(f"REASON:  {'; '.join(reason)}")

    # Save overlay
    overlay = make_overlay(g, loud, quad, outline)
    OUT_OVERLAY.parent.mkdir(parents=True, exist_ok=True)
    overlay.save(OUT_OVERLAY)
    print(f"\nwrote {OUT_OVERLAY}")

    summary = {
        "fixture": "parity_font_inter",
        "image_size": [w, h],
        "predicted_quad_rect": {
            "left": quad[0], "top": quad[1], "right": quad[2], "bottom": quad[3],
        },
        "loud_threshold": LOUD_THRESHOLD,
        "edge_threshold": EDGE_THRESHOLD,
        "near_radius_px": NEAR_PX,
        "loud_pixel_count": n_loud,
        "loud_pct": 100.0 * n_loud / (h * w),
        "glyph_outline_pixel_count": n_outline,
        "quad_perimeter_pixel_count": n_perimeter,
        "frac_loud_near_glyph_outline": frac_loud_near_outline,
        "frac_loud_near_quad_perimeter": frac_loud_near_perimeter,
        "outline_match_offset_0_0": base_score,
        "outline_best_translation": {
            "dx": dx, "dy": dy, "score": trans_score,
            "gain_vs_zero": trans_score - base_score,
        },
        "verdict": verdict,
        "reason": reason,
    }
    OUT_JSON.write_text(json.dumps(summary, indent=2))
    print(f"wrote {OUT_JSON}")


if __name__ == "__main__":
    main()
