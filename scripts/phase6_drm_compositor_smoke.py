"""Phase 3b GPU-compositor step 1 smoke — exercise DRMRenderer's new
multi-plane API on the dev Pi.

What this proves end-to-end:

  primary plane:        software-composited bg + "STATIC" white text
                        (existing render_frame path; bg + every static
                        text layer get pre-composited here once at
                        slide entry)
  animated plane 0:     "TICKER" text cropped to its glyph bbox,
                        sliding horizontally (CRTC_X update per frame)
  animated plane 1:     "PULSE" text cropped to glyph bbox,
                        alpha-modulated (alpha update per frame)

Per-frame motion = atomic-commit changing CRTC_X (ticker) and alpha
(pulse). Zero per-pixel CPU work in the inner loop. vc4 LBM
consumption per plane scales with SRC_W (the glyph bbox width), not
the full fb width — so two cropped animated planes fit comfortably
under the 1080p LBM ceiling that uncropped full-frame planes hit.

Run on the Pi (sudo for /dev/dri/card0 + DRM master):

    cd /home/openmarquee/openmarquee
    sudo PYTHONPATH=backend python3 scripts/phase6_drm_compositor_smoke.py
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from openmarquee.rendering.drm_kms import DRMRenderer  # noqa: E402

# Sign-native dims = HDMI mode. Per qarl 2026-05-02 ("stop thinking
# about low-rez for a while"), 1080p is the canonical config.
SIGN_W = 1920
SIGN_H = 1080


def _draw_text_to_glyph_bbox(
    text: str, fg: tuple[int, int, int]
) -> tuple[bytes, int, int]:
    """Render `text` in `fg` color on a transparent canvas, crop to
    the tight glyph bounding box, return (rgba_bytes, bbox_w, bbox_h).
    The crop is what keeps vc4 LBM consumption low — SRC_W on the
    animated plane is then bbox_w, not 1920."""
    # Render at large size onto an oversize canvas so even big text
    # has a known origin to crop from.
    canvas_w, canvas_h = 1920, 480
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 220,
        )
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((canvas_w - tw) / 2 - bbox[0], (canvas_h - th) / 2 - bbox[1]),
        text, fill=(*fg, 255), font=font,
    )
    glyph_bbox = img.getbbox()
    if glyph_bbox is None:
        # No ink (empty / whitespace-only); return a 1x1 transparent.
        empty = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
        return empty.tobytes(), 1, 1
    cropped = img.crop(glyph_bbox)
    return cropped.tobytes(), cropped.width, cropped.height


def _solid_rgb_with_static_text(
    bg: tuple[int, int, int], text: str, fg: tuple[int, int, int]
) -> bytes:
    """Pre-composite bg + a single static-text layer into the primary
    plane's RGB888 buffer. Mirrors what GPUSlideCompositor.attach_slide
    will do for the real slide path: software composite bg + every
    motion=static text into the primary, leave overlay planes for
    motion-animated layers only."""
    img = Image.new("RGB", (SIGN_W, SIGN_H), bg)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 200,
        )
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    # Place static text near the top of the slide so the animated
    # ticker (centered vertically) doesn't visually collide with it.
    draw.text(
        ((SIGN_W - tw) / 2 - bbox[0], 80 - bbox[1]),
        text, fill=fg, font=font,
    )
    return img.tobytes()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration", type=float, default=5.0,
        help="seconds to run the per-frame animation loop (default 5)",
    )
    args = parser.parse_args()
    card = Path("/dev/dri/card0")
    if not card.exists():
        print(f"ERR: {card} missing", file=sys.stderr)
        return 1

    print(f"opening DRMRenderer at {SIGN_W}x{SIGN_H} with 2 animated planes...")
    with DRMRenderer(
        width=SIGN_W, height=SIGN_H,
        device_path=card,
        pixel_format="xrgb8888",
        max_animated_planes=2,
    ) as r:
        print(f"  display: {r.display_width}x{r.display_height}")
        for i, slot in enumerate(r._animated_planes):
            print(f"  animated plane {i}: id={slot.plane_id}")

        # Primary plane = bg + composited static text. Software work
        # done ONCE at slide entry. Mirrors the orchestrator's actual
        # static-layer path (GPUSlideCompositor in step 2).
        print("painting primary (blue bg + composited STATIC text)...")
        r.render_frame(
            _solid_rgb_with_static_text(
                bg=(0, 51, 102), text="STATIC", fg=(255, 255, 255),
            )
        )

        # Animated layer 0: ticker text, glyph-bbox-cropped.
        ticker_bytes, ticker_w, ticker_h = _draw_text_to_glyph_bbox(
            "TICKER", (255, 80, 80),
        )
        print(f"  TICKER glyph bbox: {ticker_w}x{ticker_h} → SRC_W on plane 0")
        r.attach_animated_layer(
            0,
            ticker_bytes,
            src_w=ticker_w, src_h=ticker_h,
            crtc_x=(r.display_width - ticker_w) // 2,
            crtc_y=r.display_height // 2 - ticker_h // 2,
            crtc_w=ticker_w, crtc_h=ticker_h,
        )

        # Animated layer 1: pulse text, glyph-bbox-cropped.
        pulse_bytes, pulse_w, pulse_h = _draw_text_to_glyph_bbox(
            "PULSE", (80, 255, 80),
        )
        print(f"  PULSE  glyph bbox: {pulse_w}x{pulse_h} → SRC_W on plane 1")
        r.attach_animated_layer(
            1,
            pulse_bytes,
            src_w=pulse_w, src_h=pulse_h,
            crtc_x=(r.display_width - pulse_w) // 2,
            crtc_y=r.display_height - pulse_h - 80,
            crtc_w=pulse_w, crtc_h=pulse_h,
        )

        try:
            r.commit()
            print("  primary + 2 animated overlays = 3 planes: COMMITTED")
        except OSError as e:
            print(f"  commit FAILED ({e}) — vc4 LBM ceiling hit even with "
                  f"glyph-bbox-cropped sources. Consider smaller bboxes.")
            return 1

        print("3s eyeball: blue bg + STATIC + TICKER (mid) + PULSE (bottom)...")
        time.sleep(3)

        print(
            f"animating: ticker translates X (1 cycle / 3 s), pulse "
            f"modulates alpha (1 Hz), {args.duration:.0f}s — Ctrl-C to "
            f"stop early..."
        )
        t0 = time.perf_counter()
        n_frames = 0
        per_frame_ms: list[float] = []
        # Ticker sweep: text enters from the right (off-screen at
        # crtc_x = display_width) and exits left (off-screen at
        # crtc_x = -ticker_w). Total sweep = display_width + ticker_w
        # so the text fully clears one edge before re-appearing at
        # the other.
        ticker_total_sweep = r.display_width + ticker_w
        while time.perf_counter() - t0 < args.duration:
            f0 = time.perf_counter()
            elapsed = time.perf_counter() - t0
            # Ticker at 1 cycle / 3 s, right→left sweep.
            ticker_phase = (elapsed / 3.0) % 1.0
            ticker_x = r.display_width - int(round(ticker_phase * ticker_total_sweep))
            r.update_animated_layer(0, crtc_x=ticker_x)
            # Pulse: 1 Hz sine, alpha 30%-100%. (vc4 alpha is 0-65535
            # multiplicative on top of per-pixel alpha; default blend
            # mode is "Pre-multiplied" which honors plane.alpha.)
            pulse_phase = elapsed % 1.0
            sin01 = (math.sin(2 * math.pi * pulse_phase) + 1) / 2
            alpha = int(0.3 * 65535 + 0.7 * 65535 * sin01)
            r.update_animated_layer(1, alpha=alpha)
            r.commit()
            per_frame_ms.append((time.perf_counter() - f0) * 1000.0)
            n_frames += 1
            sleep_for = max(0, (1.0 / 30.0) - (time.perf_counter() - f0))
            if sleep_for > 0:
                time.sleep(sleep_for)

        per_frame_ms.sort()
        p95_idx = max(0, int(len(per_frame_ms) * 0.95) - 1)
        print(
            f"  {n_frames} frames in 5s — "
            f"per-frame mean={sum(per_frame_ms)/len(per_frame_ms):.2f}ms, "
            f"p95={per_frame_ms[p95_idx]:.2f}ms, "
            f"max={max(per_frame_ms):.2f}ms"
        )
        print("  (atomic commit + per-frame motion math; no per-pixel CPU work)")

        print("3s hold: ticker stopped, pulse stopped...")
        time.sleep(3)

    print("done — DRM master released.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
