#!/usr/bin/env python3
"""Phase 7 all-kinds demo — exercise every fragment shader on real seeded slides.

Loads seeded TextSlides from ContentStorage and cycles them with each
transition kind in _TRANSITION_SHADERS in turn (fade -> iris ->
dissolve -> pixelate -> scanline). Validates the new fragment shaders
landed in #197 against real openMarquee content.

Run on the dev Pi as `openmarquee` user:

  sudo killall -9 python3
  cd /home/openmarquee/openmarquee
  sudo PYTHONPATH=backend python3 scripts/phase7_all_kinds_demo.py
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
from openmarquee.rendering.shader_compositor import (  # noqa: E402
    ShaderRenderer,
    _TRANSITION_SHADERS,
)
from openmarquee.rendering.snapshot import compose_slide_rgba  # noqa: E402

DATA_ROOT = Path("/home/openmarquee/data")
CONTENT_DIR = DATA_ROOT / "content"
SIGN_W = 1920
SIGN_H = 1080
MAX_ANIMATED_PLANES = 8
_FADE_FPS = 30

log = logging.getLogger("phase7_all_kinds_demo")


def _rgba_to_rgb(rgba: bytes, w: int, h: int) -> bytes:
    import numpy as np
    arr = np.frombuffer(rgba, dtype=np.uint8).reshape(h, w, 4)
    return arr[:, :, :3].tobytes()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-slide-seconds", type=float, default=2.5)
    parser.add_argument("--transition-ms", type=int, default=1200)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    kinds = sorted(_TRANSITION_SHADERS.keys())
    log.info("cycling through transition kinds: %s", kinds)

    content = ContentStorage(CONTENT_DIR)
    text_slides = sorted(
        (s for s in content.list_all() if isinstance(s, TextSlide)),
        key=lambda s: str(s.id),
    )
    if len(text_slides) < 2:
        raise RuntimeError(f"need 2+ TextSlides; found {len(text_slides)}")

    with DRMRenderer(
        SIGN_W, SIGN_H,
        pixel_format="rgb565",
        max_animated_planes=MAX_ANIMATED_PLANES,
    ) as drm:
        log.info("DRMRenderer up: %dx%d fd=%d", drm.width, drm.height, drm.drm_fd)

        log.info("pre-composing %d slide snapshots...", len(text_slides))
        snapshots: list[tuple[bytes, bytes]] = []
        t0 = time.monotonic()
        for s in text_slides:
            rgba = compose_slide_rgba(
                s, drm.width, drm.height, read_asset=content.read_asset,
            )
            snapshots.append((rgba, _rgba_to_rgb(rgba, drm.width, drm.height)))
        log.info(
            "composed %d snapshots in %.1f ms",
            len(snapshots), (time.monotonic() - t0) * 1000,
        )

        shared_fd = drm.drm_fd
        assert shared_fd is not None
        shader = ShaderRenderer(drm_fd=shared_fd).__enter__()
        try:
            log.info("ShaderRenderer up via shared fd=%d", shared_fd)

            # Show first slide via DRMRenderer.
            drm.render_frame(snapshots[0][1])
            drm.commit()
            time.sleep(args.per_slide_seconds)

            # Cycle: each transition uses a different kind, walking
            # through all kinds in order. Slides repeat as needed.
            n_slides = len(snapshots)
            for trans_idx, kind in enumerate(kinds):
                from_idx = trans_idx % n_slides
                to_idx = (trans_idx + 1) % n_slides
                log.info(
                    "transition %d via %s: slide %d -> slide %d",
                    trans_idx, kind, from_idx, to_idx,
                )

                shader.set_kind(kind)
                shader.set_from(snapshots[from_idx][0], drm.width, drm.height)
                shader.set_to(snapshots[to_idx][0], drm.width, drm.height)

                n_frames = max(1, int(args.transition_ms / 1000 * _FADE_FPS))
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
                    "%s: %d frames in %.2fs (%.1f fps)",
                    kind, n_frames, elapsed, n_frames / elapsed,
                )

                # Hand primary back to DRMRenderer for steady-state.
                drm.render_frame(snapshots[to_idx][1])
                drm.restage_primary_fb()
                drm.commit()
                time.sleep(args.per_slide_seconds)
        finally:
            shader.close()

    log.info("clean teardown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
