#!/usr/bin/env python3
"""Phase 7 ShaderRenderer Milestone A smoke test.

Drives the new ShaderRenderer with a single 1080p RGBA bg image for
N seconds, page-flipping every 33 ms (~30 fps). Validates the
production module's DRM/EGL/GBM/GL teardown behavior + bg-only
rendering pipeline before Milestone B layers in per-layer textures
and motion uniforms.

Run on the dev Pi as `openmarquee` user. Requires DRM master, so the
welcome loop must be stopped first:

  sudo pkill -f phase6_welcome_loop.py
  cd /home/openmarquee/openmarquee
  sudo PYTHONPATH=backend python3 scripts/phase7_shader_renderer_smoke.py
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from PIL import Image, ImageDraw, ImageFont

from openmarquee.rendering.shader_compositor import ShaderRenderer

log = logging.getLogger("phase7_smoke")


def _build_test_bg(width: int, height: int) -> bytes:
    """A test bg with text + a smooth gradient + corner color markers.
    Expected on-glass result (after the vertex shader's Y-flip):
        TL=red, TR=green, BL=blue, BR=yellow corners
        diagonal gradient: bottom-left dark → top-right bright
        white "PHASE 7 / shader compositor" text, centered
    A wrong-stride or wrong-orientation bug shows up immediately."""
    import numpy as np

    # Smooth gradient via numpy (Python pixel-loop is ~2 s on Pi at
    # 1080p; numpy is < 50 ms).
    xs = np.linspace(0.0, 1.0, width, dtype=np.float32)
    ys = np.linspace(0.0, 1.0, height, dtype=np.float32)
    r = np.broadcast_to((xs * 255).astype(np.uint8)[None, :], (height, width))
    g = np.broadcast_to((ys * 255).astype(np.uint8)[:, None], (height, width))
    b = np.full((height, width), 64, dtype=np.uint8)
    a = np.full((height, width), 255, dtype=np.uint8)
    img = Image.fromarray(np.stack([r, g, b, a], axis=-1).copy(), mode="RGBA")

    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 96)
    except OSError:
        font = ImageFont.load_default()
    draw.text(
        (width // 2, height // 2),
        "PHASE 7\nshader compositor",
        fill=(255, 255, 255, 255),
        font=font,
        anchor="mm",
        align="center",
    )
    draw.rectangle([0, 0, 24, 24], fill=(255, 0, 0, 255))
    draw.rectangle([width - 24, 0, width, 24], fill=(0, 255, 0, 255))
    draw.rectangle([0, height - 24, 24, height], fill=(0, 0, 255, 255))
    draw.rectangle(
        [width - 24, height - 24, width, height], fill=(255, 255, 0, 255),
    )
    return img.tobytes()


def _build_text_layer(
    text: str,
    font_size: int,
    color: tuple[int, int, int, int],
    outline: tuple[int, int, int, int] | None = None,
) -> tuple[bytes, int, int]:
    """Render text into a glyph-bbox-cropped RGBA bitmap. Returns
    (rgba_bytes, w, h) so the smoke can pass src dims to
    ShaderRenderer.attach_layer."""
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            font_size,
        )
    except OSError:
        font = ImageFont.load_default()
    # Render onto a generous canvas, then crop to ink bbox.
    pad = font_size  # outline strokes can extend past the ascender
    bbox = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox(
        (0, 0), text, font=font, stroke_width=4 if outline else 0,
    )
    canvas_w = bbox[2] - bbox[0] + 2 * pad
    canvas_h = bbox[3] - bbox[1] + 2 * pad
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.text(
        (pad - bbox[0], pad - bbox[1]), text, font=font, fill=color,
        stroke_width=4 if outline else 0,
        stroke_fill=outline if outline else None,
    )
    crop = img.getbbox()
    if crop is None:
        raise RuntimeError("text rendered to no ink")
    img = img.crop(crop)
    return (img.tobytes(), img.width, img.height)


def main() -> int:
    parser = argparse.ArgumentParser(description="ShaderRenderer milestone A smoke")
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--fps", type=float, default=30.0)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    with ShaderRenderer() as r:
        log.info("renderer up: %dx%d", r.width, r.height)
        bg = _build_test_bg(r.width, r.height)
        r.set_background(bg)

        # Milestone B: stack a few text layers on top of bg via the
        # multi-slot blend ladder. Each layer is a glyph-bbox-cropped
        # RGBA + a placement rect in display UV [0,1].
        big = _build_text_layer(
            "OPEN", font_size=420, color=(255, 255, 255, 255),
            outline=(0, 0, 0, 200),
        )
        r.attach_layer(
            1, big[0], big[1], big[2],
            dst_x=0.20, dst_y=0.18, dst_w=0.60, dst_h=0.30,
            opacity=1.0,
        )
        sub = _build_text_layer(
            "for shader testing", font_size=88,
            color=(255, 230, 80, 255),
        )
        r.attach_layer(
            2, sub[0], sub[1], sub[2],
            dst_x=0.10, dst_y=0.55, dst_w=0.80, dst_h=0.10,
            opacity=0.95,
        )
        ribbon = _build_text_layer(
            "Phase 7 Milestone B  -  multi-layer blend on glass",
            font_size=56, color=(255, 255, 255, 255),
            outline=(40, 40, 40, 255),
        )
        r.attach_layer(
            3, ribbon[0], ribbon[1], ribbon[2],
            dst_x=0.05, dst_y=0.85, dst_w=0.90, dst_h=0.07,
            opacity=0.85,
        )

        n_frames = int(args.seconds * args.fps)
        frame_dt = 1.0 / args.fps
        t0 = time.monotonic()
        for i in range(n_frames):
            r.commit_frame()
            target = t0 + (i + 1) * frame_dt
            sleep_for = target - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
        elapsed = time.monotonic() - t0
        log.info(
            "drew %d frames in %.2fs (%.1f fps)",
            n_frames, elapsed, n_frames / elapsed,
        )

    log.info("clean teardown — display blanked, DRM master released")
    return 0


if __name__ == "__main__":
    sys.exit(main())
