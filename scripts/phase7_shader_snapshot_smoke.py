#!/usr/bin/env python3
"""Phase 7 shader-compositor integration smoke — fade two seeded slides.

Loads two real TextSlides from ContentStorage (the Welcome playlist
slides seeded into /home/openmarquee/data/content), composes each as
a 1080p RGBA snapshot via openmarquee.rendering.snapshot.compose_slide_
rgba, and runs a fade transition between them on the dev Pi via
ShaderRenderer.

Validates the full path: real slide -> snapshot -> shader transition
on glass. Companion to phase7_shader_renderer_smoke.py (which uses
synthetic test slides built in-memory).

Run on the dev Pi as `openmarquee` user. Requires DRM master, so the
welcome loop must be stopped first:

  sudo killall -9 python3
  cd /home/openmarquee/openmarquee
  sudo PYTHONPATH=backend python3 scripts/phase7_shader_snapshot_smoke.py
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
from openmarquee.rendering.shader_compositor import ShaderRenderer  # noqa: E402
from openmarquee.rendering.snapshot import compose_slide_rgba  # noqa: E402

DATA_ROOT = Path("/home/openmarquee/data")
CONTENT_DIR = DATA_ROOT / "content"

log = logging.getLogger("phase7_snapshot_smoke")


def _pick_two_text_slides(content: ContentStorage) -> tuple[TextSlide, TextSlide]:
    """Pull two text slides out of the seeded content. The Welcome
    playlist seeds three (Welcome / to / openMarquee) plus the
    bundled image and video slides. We want any two text slides;
    sorted by id for determinism so the smoke is reproducible across
    runs."""
    items = content.list_all()
    text_slides = [
        item for item in items if isinstance(item, TextSlide)
    ]
    if len(text_slides) < 2:
        raise RuntimeError(
            f"need at least 2 TextSlides in {CONTENT_DIR}; found "
            f"{len(text_slides)}. Seed first: run "
            f"scripts/phase6_welcome_loop.py briefly to populate content."
        )
    text_slides.sort(key=lambda s: str(s.id))
    return text_slides[0], text_slides[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="ShaderRenderer + snapshot smoke")
    parser.add_argument("--seconds", type=float, default=2.0,
                        help="duration of the forward fade (also the reverse)")
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--kind", default="fade")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    content = ContentStorage(CONTENT_DIR)
    slide_a, slide_b = _pick_two_text_slides(content)
    log.info("picked slides: a=%s b=%s", slide_a.id, slide_b.id)

    with ShaderRenderer() as r:
        log.info("renderer up: %dx%d", r.width, r.height)

        # Compose each slide as a 1080p RGBA. Cost is one PIL bg load
        # + one alpha_composite per visible layer per slide; ~50-100 ms
        # per slide on the dev Pi at 1080p (one-time at transition entry).
        t = time.monotonic()
        snap_a = compose_slide_rgba(
            slide_a, r.width, r.height, read_asset=content.read_asset,
        )
        snap_b = compose_slide_rgba(
            slide_b, r.width, r.height, read_asset=content.read_asset,
        )
        log.info("composed both snapshots in %.1f ms", (time.monotonic() - t) * 1000)

        r.set_kind(args.kind)
        r.set_from(snap_a, r.width, r.height)
        r.set_to(snap_b, r.width, r.height)

        n_frames = int(args.seconds * args.fps)
        frame_dt = 1.0 / args.fps

        # First pass: hold slide A briefly, fade to slide B.
        for hold_t in range(int(args.fps)):  # ~1 s hold at the start
            r.set_transition_t(0.0)
            r.commit_frame()
            time.sleep(frame_dt)

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
            "%s a->b: %d frames in %.2fs (%.1f fps)",
            args.kind, n_frames, elapsed, n_frames / elapsed,
        )

        # Hold slide B briefly so qarl can see the result.
        for hold_t in range(int(args.fps)):
            r.set_transition_t(1.0)
            r.commit_frame()
            time.sleep(frame_dt)

    log.info("clean teardown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
