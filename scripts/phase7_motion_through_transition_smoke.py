#!/usr/bin/env python3
"""Glass test for #206 — animated text keeps moving through shader transition.

Constructs two in-memory TextSlides (no persistence touched):

  slide A: bg + a "ticker" motion layer with long scrolling text.
           Continuous horizontal scroll = the most visually obvious
           motion to verify continues during the transition.
  slide B: bg + a static text layer (no motion). Plain target.

Drives PlaybackLoop directly with fetch_items returning these two
slides, transition kind = iris (routes through the shader path with
OPENMARQUEE_SHADER_TRANSITIONS=1). Plays for ~30 s so we cycle the
playlist a few times and see the iris transition fire repeatedly.

Expected on glass:
  - During slide A's ~5 s steady state: ticker scrolls continuously.
  - As iris transition begins: ticker KEEPS SCROLLING through the
    transition window (~500 ms), fading out via plane.alpha as the
    iris circle reveals slide B underneath.
  - Slide B appears with static text.
  - Loop continues.

Without the #206 wiring this prints would show: ticker freezes the
moment the iris transition starts (snapshot-baked into u_from), and
iris reveals a static slide B underneath. With #206: ticker
continues scrolling DURING the iris.

Run on the dev Pi:

  sudo killall -9 python3
  cd /home/openmarquee/openmarquee
  sudo PYTHONPATH=backend OPENMARQUEE_SHADER_TRANSITIONS=1 python3 \
       scripts/phase7_motion_through_transition_smoke.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(ROOT))

import io  # noqa: E402

from PIL import Image  # noqa: E402

from openmarquee.content import TextBox, TextLayer, TextSlide  # noqa: E402
from openmarquee.playback import PlaybackLoop  # noqa: E402
from openmarquee.rendering.drm_kms import DRMRenderer  # noqa: E402
from openmarquee.rendering.snapshot import compose_slide_rgba  # noqa: E402

SIGN_W = 1920
SIGN_H = 1080
MAX_ANIMATED_PLANES = 8

log = logging.getLogger("phase7_motion_smoke")


def _build_ticker_slide() -> TextSlide:
    """Slide A: long ticker text + a static headline layer."""
    return TextSlide(
        id=uuid4(),
        name="motion-ticker-test",
        background_color="#0a1a2e",
        text_layers=[
            TextLayer(
                text="HEADLINE STAYS PUT",
                name="headline",
                font_size_pct=12.0,
                text_color="#ffffff",
                box=TextBox(x=0.1, y=0.1, w=0.8, h=0.2),
                motion="static",
            ),
            TextLayer(
                text=(
                    "TICKER TICKER TICKER -- this should keep scrolling "
                    "even when the iris transition fires -- if it freezes "
                    "during the transition, #206 is broken -- if it keeps "
                    "moving through the transition window, #206 works"
                ),
                name="ticker",
                font_size_pct=8.0,
                text_color="#ffe040",
                box=TextBox(x=0.05, y=0.5, w=0.9, h=0.3),
                motion="ticker",
            ),
        ],
    )


def _build_static_slide() -> TextSlide:
    """Slide B: a different ticker direction so every transition
    exercises the with-motion path -- snapshot-only would mask the
    very perf delta we want to verify."""
    return TextSlide(
        id=uuid4(),
        name="motion-target",
        background_color="#2e0a1a",
        text_layers=[
            TextLayer(
                text="SLIDE B HEADER",
                name="header",
                font_size_pct=12.0,
                text_color="#ffffff",
                box=TextBox(x=0.1, y=0.1, w=0.8, h=0.2),
                motion="static",
            ),
            TextLayer(
                text=(
                    "second slide ticker -- different content so you "
                    "can tell which direction the transition is going"
                ),
                name="ticker-b",
                font_size_pct=8.0,
                text_color="#a0d0ff",
                box=TextBox(x=0.05, y=0.5, w=0.9, h=0.3),
                motion="ticker",
            ),
        ],
    )


async def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--duration", type=float, default=600.0,
        help="seconds to run before stopping (default 600 = 10 min)",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    slide_a = _build_ticker_slide()
    slide_b = _build_static_slide()

    # Patch transition fields onto each slide -- PlaybackLoop reads
    # `item.transition` and `item.transition_ms` directly (these live
    # on ContentItem after list_in_playlist_order patches them).
    slide_a_with_trans = slide_a.model_copy(
        update={"transition": "iris", "transition_ms": 1000, "duration_ms": 4000},
    )
    slide_b_with_trans = slide_b.model_copy(
        update={"transition": "iris", "transition_ms": 1000, "duration_ms": 4000},
    )

    # Pre-render both slides as PNG bytes for _read_asset. PlaybackLoop
    # calls _read_asset(item.id) when (a) static slides need their
    # asset.png loaded for display, and (b) the next-image step of the
    # transition needs the incoming slide as an Image. Slide A is
    # dynamic (GPUSlideCompositor.attach renders from text_layers
    # directly, no asset.png needed), but slide B is static and the
    # transition's next_image always loads via _safe_load_image too.
    asset_cache: dict[UUID, bytes] = {}
    for slide in (slide_a, slide_b):
        rgba = compose_slide_rgba(slide, SIGN_W, SIGN_H, read_asset=None)
        img = Image.frombytes("RGBA", (SIGN_W, SIGN_H), rgba).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        asset_cache[slide.id] = buf.getvalue()
    log.info("pre-rendered %d slide assets", len(asset_cache))

    def _read_asset(item_id: UUID) -> bytes:
        if item_id in asset_cache:
            return asset_cache[item_id]
        raise FileNotFoundError(f"unknown asset {item_id}")

    fetch_items_called = 0
    def _fetch_items() -> list:
        nonlocal fetch_items_called
        fetch_items_called += 1
        return [slide_a_with_trans, slide_b_with_trans]

    with DRMRenderer(
        SIGN_W, SIGN_H,
        pixel_format="rgb565",
        max_animated_planes=MAX_ANIMATED_PLANES,
    ) as drm:
        log.info("DRMRenderer up: %dx%d fd=%d", drm.width, drm.height, drm.drm_fd)

        loop = PlaybackLoop(
            renderer=drm,
            fetch_items=_fetch_items,
            read_asset=_read_asset,
        )
        await loop.start()
        log.info(
            "playback loop started; cycling for %.0f s -- watch the screen "
            "(Ctrl-C to stop earlier)", args.duration,
        )
        try:
            await asyncio.sleep(args.duration)
        except KeyboardInterrupt:
            log.info("interrupted; stopping cleanly")
        finally:
            await loop.stop()
            log.info("loop stopped after %d fetch_items calls", fetch_items_called)
    log.info("clean teardown")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
