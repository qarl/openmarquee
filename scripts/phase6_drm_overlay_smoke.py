"""Phase 2a-2 DRM/KMS overlay smoke test — multi-plane composite.

Demonstrates the GPU compositor: a colored background on the primary
plane, alpha-blended text on the overlay plane, GPU mixes them at
scanout. Per-frame cost is the overlay buffer rewrite only — no
software alpha blend, no software composite.

Run on the Pi (sudo for /dev/dri/card0 + DRM master):

    cd /home/openmarquee/openmarquee
    sudo PYTHONPATH=backend python3 scripts/phase6_drm_overlay_smoke.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from openmarquee.rendering.drm_kms import DRMRenderer  # noqa: E402

SIGN_W = 128
SIGN_H = 96
FPS_FRAMES = 30


def _solid_rgb(w: int, h: int, color: tuple[int, int, int]) -> bytes:
    """RGB888 buffer filled with a single color — sign-side primary content."""
    return Image.new("RGB", (w, h), color).tobytes()


def _text_rgba(w: int, h: int, text: str, fg: tuple[int, int, int, int]) -> bytes:
    """RGBA8888 buffer with `text` drawn opaque-fg on transparent ground.
    The transparent ground lets the primary plane show through the rest
    of the overlay plane."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Use the largest dejavu font that fits — keep it dead simple.
    for size in (32, 28, 24, 20, 16, 12):
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size
            )
        except OSError:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        if tw <= w - 4 and th <= h - 4:
            break
    x = (w - tw) // 2 - bbox[0]
    y = (h - th) // 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=fg)
    return img.tobytes()


def main() -> int:
    card = Path("/dev/dri/card0")
    if not card.exists():
        print(f"ERR: {card} missing — DRM not available", file=sys.stderr)
        return 1

    print(f"rendering primary (solid blue) and overlay text at {SIGN_W}x{SIGN_H}…")
    primary = _solid_rgb(SIGN_W, SIGN_H, (0, 51, 102))
    overlay = _text_rgba(SIGN_W, SIGN_H, "OVERLAY", (255, 255, 255, 255))

    print(f"opening DRM renderer with overlay enabled ({card})…")
    with DRMRenderer(
        width=SIGN_W, height=SIGN_H, enable_overlay=True, device_path=card
    ) as r:
        print(f"display: {r.display_width}x{r.display_height}")
        print("pushing initial composite (primary + overlay)…")
        r.render_composite(primary_rgb=primary, overlay_rgba=overlay)

        # Hold so a human can confirm the GPU composite landed.
        print("holding 3s — eyeball: blue background + white OVERLAY text…")
        time.sleep(3)

        # Now exercise the per-frame overlay-update path: text rotates
        # through three labels. Primary plane stays blue (no per-frame
        # primary write), so this measures the overlay-only cost — the
        # path that matters for "live-updating text over background".
        print(f"running {FPS_FRAMES} overlay-only updates to measure fps…")
        labels = ["FRAME", "OVER", "GPU"]
        overlays = [
            _text_rgba(SIGN_W, SIGN_H, lbl, (255, 255, 255, 255))
            for lbl in labels
        ]
        t0 = time.perf_counter()
        for i in range(FPS_FRAMES):
            r.render_composite(overlay_rgba=overlays[i % len(overlays)])
        elapsed = time.perf_counter() - t0
        per_frame = (elapsed / FPS_FRAMES) * 1000
        print(
            f"  overlay-only per-frame: {per_frame:.1f} ms "
            f"({1000 / per_frame:.1f} fps)"
        )

        print("holding 3s — eyeball: text should be the LAST label drawn…")
        time.sleep(3)

    print("done — DRM master released; original CRTC restored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
