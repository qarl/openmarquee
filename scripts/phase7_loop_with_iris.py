#!/usr/bin/env python3
"""Phase 7 multi-slide integration — DRMRenderer steady-state + ShaderRenderer iris transitions.

Loads three seeded TextSlides from ContentStorage and cycles them with
an iris transition between each. Demonstrates the full transition path
on real openMarquee content:

  for each slide:
    DRMRenderer.render_frame(slide_rgb) + commit  # bg+statics on primary
    hold 2 s
    if next slide:
      ShaderRenderer (shared fd) iris transition over 1 s
      DRMRenderer.restage_primary_fb() + commit   # hand primary back

Validates the same dance #196 will run inside PlaybackLoop, without
touching playback.py. Once this is solid, the wire-up to _iris is a
straight refactor: pull the transition body into a method, gate
behind a feature flag.

Run on the dev Pi:

  sudo killall -9 python3
  cd /home/openmarquee/openmarquee
  sudo PYTHONPATH=backend python3 scripts/phase7_loop_with_iris.py
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(ROOT))

from openmarquee.content import TextSlide  # noqa: E402
from openmarquee.content.storage import ContentStorage  # noqa: E402
from openmarquee.rendering.drm_kms import DRMRenderer  # noqa: E402
from openmarquee.rendering.shader_compositor import ShaderRenderer  # noqa: E402
from openmarquee.rendering.snapshot import compose_slide_rgba  # noqa: E402

DATA_ROOT = Path("/home/openmarquee/data")
CONTENT_DIR = DATA_ROOT / "content"

SIGN_W = 1920
SIGN_H = 1080
MAX_ANIMATED_PLANES = 8

_FADE_FPS = 30

log = logging.getLogger("phase7_loop_with_iris")


def _rgba_to_rgb(rgba: bytes, w: int, h: int) -> bytes:
    """Drop the alpha channel for DRMRenderer.render_frame, which
    accepts RGB888 only. ContentStorage seed assets compose to RGBA;
    DRMRenderer's primary plane is rgb565 + an internal convert that
    operates on the RGB888 input."""
    import numpy as np
    arr = np.frombuffer(rgba, dtype=np.uint8).reshape(h, w, 4)
    return arr[:, :, :3].tobytes()


def _run_iris_transition(
    drm: DRMRenderer,
    shader: ShaderRenderer,
    from_rgba: bytes,
    to_rgba: bytes,
    next_rgb: bytes,
    transition_ms: int,
) -> None:
    """One iris shader transition, then hand primary back to DRM.

    Mirrors what PlaybackLoop._iris_shader will do once #196 wires
    this in: drive the GBM-backed primary fb through the transition,
    then restage_primary_fb + commit so DRMRenderer reclaims the
    primary plane for steady-state scanout of the next slide.
    """
    shader.set_kind("iris")
    shader.set_from(from_rgba, drm.width, drm.height)
    shader.set_to(to_rgba, drm.width, drm.height)
    n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
    frame_dt = 1.0 / _FADE_FPS
    t0 = time.monotonic()
    for i in range(n_frames):
        t = i / max(1, n_frames - 1)
        shader.set_transition_t(t)
        shader.commit_frame()
        target = t0 + (i + 1) * frame_dt
        sleep_for = target - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
    elapsed = time.monotonic() - t0
    log.info(
        "iris transition: %d frames in %.2fs (%.1f fps)",
        n_frames, elapsed, n_frames / elapsed,
    )

    # Hand primary back to DRMRenderer in one atomic commit. Order
    # is load-bearing: paint dumb buffer first, restage primary FB_ID,
    # commit, THEN release shader's fbs (pending_bo cleanup happens
    # on the NEXT commit_frame OR ShaderRenderer.close).
    drm.render_frame(next_rgb)
    drm.restage_primary_fb()
    drm.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-slide-seconds", type=float, default=2.0)
    parser.add_argument("--transition-ms", type=int, default=1000)
    parser.add_argument("--cycles", type=int, default=2)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    content = ContentStorage(CONTENT_DIR)
    text_slides = [
        item for item in content.list_all() if isinstance(item, TextSlide)
    ]
    text_slides.sort(key=lambda s: str(s.id))
    if len(text_slides) < 2:
        raise RuntimeError(
            f"need at least 2 TextSlides; found {len(text_slides)}. "
            f"Run scripts/phase6_welcome_loop.py briefly to seed."
        )
    log.info(
        "loaded %d text slides; cycling through them",
        len(text_slides),
    )

    with DRMRenderer(
        SIGN_W, SIGN_H,
        pixel_format="rgb565",
        max_animated_planes=MAX_ANIMATED_PLANES,
    ) as drm:
        log.info("DRMRenderer up: %dx%d fd=%d", drm.width, drm.height, drm.drm_fd)

        # Pre-compose every slide's RGBA snapshot once so per-transition
        # cost is the shader work, not the PIL composite. This is the
        # shape #205's snapshot cache will land in production.
        log.info("pre-composing %d slide snapshots...", len(text_slides))
        t = time.monotonic()
        snapshots: list[tuple[bytes, bytes]] = []  # (rgba, rgb) per slide
        for slide in text_slides:
            rgba = compose_slide_rgba(
                slide, drm.width, drm.height,
                read_asset=content.read_asset,
            )
            rgb = _rgba_to_rgb(rgba, drm.width, drm.height)
            snapshots.append((rgba, rgb))
        log.info(
            "composed %d snapshots in %.1f ms",
            len(snapshots), (time.monotonic() - t) * 1000,
        )

        # Open ShaderRenderer once at startup, reuse for every
        # transition. EGL/GL init is ~5 s on the dev Pi (cold mesa
        # cache); doing it once amortizes across the whole session.
        shared_fd = drm.drm_fd
        assert shared_fd is not None
        shader = ShaderRenderer(drm_fd=shared_fd).__enter__()
        try:
            log.info(
                "ShaderRenderer up via shared fd=%d, %dx%d",
                shared_fd, shader.width, shader.height,
            )

            # Show first slide via DRMRenderer.
            drm.render_frame(snapshots[0][1])
            drm.commit()
            log.info(
                "slide 0 (id=%s) on screen", str(text_slides[0].id)[:8],
            )

            for cycle in range(args.cycles):
                for i, (rgba, rgb) in enumerate(snapshots):
                    if cycle == 0 and i == 0:
                        # Already on screen from initial paint.
                        time.sleep(args.per_slide_seconds)
                        continue
                    next_idx = i
                    prev_idx = (i - 1) % len(snapshots)
                    log.info(
                        "transition %d -> %d (cycle %d)",
                        prev_idx, next_idx, cycle,
                    )
                    _run_iris_transition(
                        drm, shader,
                        snapshots[prev_idx][0], snapshots[next_idx][0],
                        snapshots[next_idx][1],
                        args.transition_ms,
                    )
                    log.info(
                        "slide %d (id=%s) on screen",
                        next_idx, str(text_slides[next_idx].id)[:8],
                    )
                    time.sleep(args.per_slide_seconds)
        finally:
            shader.close()
            log.info("ShaderRenderer closed")

    log.info("clean teardown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
