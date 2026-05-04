#!/usr/bin/env python3
"""Phase 7 ShaderRenderer transition smoke test.

Drives the shader compositor with two test slide snapshots and
animates a fade transition between them over N seconds. Validates
the 2-input + transition_t hot path that's the entire job of the
shader compositor in the hybrid architecture (multi-plane DRM keeps
within-slide layer compositing; shaders only run during transitions
and Photoshop blend modes).

Run on the dev Pi as `openmarquee` user. Requires DRM master, so the
welcome loop must be stopped first:

  sudo killall -9 python3
  cd /home/openmarquee/openmarquee
  sudo PYTHONPATH=backend python3 scripts/phase7_shader_renderer_smoke.py
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from openmarquee.rendering.shader_compositor import ShaderRenderer

log = logging.getLogger("phase7_smoke")


def _try_font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size,
        )
    except OSError:
        return ImageFont.load_default()


def _build_slide_a(width: int, height: int) -> bytes:
    """First slide: warm sunset gradient with "WELCOME" text."""
    xs = np.linspace(0.0, 1.0, width, dtype=np.float32)
    ys = np.linspace(0.0, 1.0, height, dtype=np.float32)
    r = np.broadcast_to(((1.0 - ys) * 240 + 15).astype(np.uint8)[:, None], (height, width))
    g = np.broadcast_to(((1.0 - ys) * 100 + 30).astype(np.uint8)[:, None], (height, width))
    b = np.broadcast_to(((ys) * 80 + 20).astype(np.uint8)[:, None], (height, width))
    a = np.full((height, width), 255, dtype=np.uint8)
    img = Image.fromarray(np.stack([r, g, b, a], axis=-1).copy(), mode="RGBA")

    draw = ImageDraw.Draw(img)
    draw.text(
        (width // 2, height // 2 - 80), "WELCOME",
        fill=(255, 255, 255, 255), font=_try_font(280),
        anchor="mm", stroke_width=4, stroke_fill=(60, 20, 0, 255),
    )
    draw.text(
        (width // 2, height // 2 + 140), "to openMarquee",
        fill=(255, 230, 200, 255), font=_try_font(80), anchor="mm",
    )
    return img.tobytes()


def _build_slide_b(width: int, height: int) -> bytes:
    """Second slide: cool deep-blue gradient with "OPEN" text."""
    xs = np.linspace(0.0, 1.0, width, dtype=np.float32)
    ys = np.linspace(0.0, 1.0, height, dtype=np.float32)
    r = np.broadcast_to((ys * 30 + 5).astype(np.uint8)[:, None], (height, width))
    g = np.broadcast_to((ys * 60 + 30).astype(np.uint8)[:, None], (height, width))
    b = np.broadcast_to(((1.0 - ys) * 120 + 80).astype(np.uint8)[:, None], (height, width))
    a = np.full((height, width), 255, dtype=np.uint8)
    img = Image.fromarray(np.stack([r, g, b, a], axis=-1).copy(), mode="RGBA")

    draw = ImageDraw.Draw(img)
    draw.text(
        (width // 2, height // 2), "OPEN",
        fill=(255, 255, 255, 255), font=_try_font(420),
        anchor="mm", stroke_width=6, stroke_fill=(20, 40, 80, 255),
    )
    draw.text(
        (width // 2, height // 2 + 220), "phase 7 transition",
        fill=(180, 220, 255, 255), font=_try_font(64), anchor="mm",
    )
    return img.tobytes()


def main() -> int:
    parser = argparse.ArgumentParser(description="ShaderRenderer transition smoke")
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--kind", default="fade", help="transition kind")
    parser.add_argument("--reverse", action="store_true",
                        help="Run the transition twice (A->B then B->A)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    with ShaderRenderer() as r:
        log.info("renderer up: %dx%d", r.width, r.height)
        slide_a = _build_slide_a(r.width, r.height)
        slide_b = _build_slide_b(r.width, r.height)

        r.set_kind(args.kind)
        r.set_from(slide_a, r.width, r.height)
        r.set_to(slide_b, r.width, r.height)

        n_frames = int(args.seconds * args.fps)
        frame_dt = 1.0 / args.fps
        t0 = time.monotonic()
        for i in range(n_frames):
            t = i / max(1, n_frames - 1)  # 0..1 over the run
            r.set_transition_t(t)
            r.commit_frame()
            target = t0 + (i + 1) * frame_dt
            sleep_for = target - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
        elapsed = time.monotonic() - t0
        log.info(
            "%s transition: %d frames in %.2fs (%.1f fps)",
            args.kind, n_frames, elapsed, n_frames / elapsed,
        )

        if args.reverse:
            # Demonstrate that swapping inputs runs the same transition
            # in reverse — useful sanity check that set_from/set_to
            # don't have stale-bind issues on second use.
            r.set_from(slide_b, r.width, r.height)
            r.set_to(slide_a, r.width, r.height)
            t0 = time.monotonic()
            for i in range(n_frames):
                t = i / max(1, n_frames - 1)
                r.set_transition_t(t)
                r.commit_frame()
                target = t0 + (i + 1) * frame_dt
                sleep_for = target - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)
            elapsed = time.monotonic() - t0
            log.info(
                "%s reverse: %d frames in %.2fs (%.1f fps)",
                args.kind, n_frames, elapsed, n_frames / elapsed,
            )

    log.info("clean teardown — display blanked, DRM master released")
    return 0


if __name__ == "__main__":
    sys.exit(main())
