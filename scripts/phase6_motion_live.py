"""Phase 3b GPU-compositor step 4 live-fire harness — drive a motion-
rich TextSlide through the FULL PlaybackLoop + DRMRenderer +
GPUSlideCompositor stack on the dev Pi.

The welcome loop's seeded playlist (scripts/phase6_welcome_loop.py)
is all static text + image / video content; nothing in it exercises
the new GPU path. This script constructs a synthetic playlist with
the six motion effects + an auto-mode clock + a static text overlay,
all on one slide that loops, so the GPU compositor's attach / tick /
detach lifecycle actually fires on hardware.

Run on the Pi (sudo for /dev/dri/card0 + DRM master):

    cd /home/openmarquee/openmarquee
    sudo PYTHONPATH=backend python3 scripts/phase6_motion_live.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(ROOT))

from openmarquee.content import TextBox, TextLayer, TextSlide  # noqa: E402
from openmarquee.playback import PlaybackLoop  # noqa: E402
from openmarquee.rendering.drm_kms import DRMRenderer  # noqa: E402

SIGN_W = 1920
SIGN_H = 1080
MAX_ANIMATED_PLANES = 8


def _layer(
    text: str,
    motion: str,
    *,
    intensity: int = 80,
    box: tuple[float, float, float, float],
    color: str = "#FFFFFF",
    auto_mode: str | None = None,
    motion_phase: float = 0.0,
) -> TextLayer:
    return TextLayer(
        text=text,
        motion=motion,
        motion_intensity=intensity,
        motion_phase=motion_phase,
        text_color=color,
        box=TextBox(x=box[0], y=box[1], w=box[2], h=box[3]),
        auto_mode=auto_mode,
        font_size_px=120,
    )


def _build_slides() -> list[TextSlide]:
    """One slide per effect so the operator can eyeball them in
    sequence on glass. Each slide carries 1-2 animated layers + 1
    static label so the GPU compositor exercises both the primary-
    plane software composite (static + bg) AND the per-effect plane
    property updates."""
    slides: list[TextSlide] = []

    def make(name: str, layers: list[TextLayer], duration_ms: int = 4000) -> TextSlide:
        return TextSlide(
            id=uuid4(),
            name=name,
            text_layers=layers,
            duration_ms=duration_ms,
            background_color="#0A1F33",
        )

    slides.append(make("ticker", [
        _layer("ticker", "static", box=(0.05, 0.05, 0.9, 0.15), color="#FFCC66"),
        _layer("BREAKING NEWS — GPU COMPOSITOR LIVE",
               "ticker", intensity=70, box=(0.05, 0.4, 0.9, 0.2), color="#FF6666"),
    ]))
    slides.append(make("breathe", [
        _layer("breathe", "static", box=(0.05, 0.05, 0.9, 0.15), color="#FFCC66"),
        _layer("BREATHE",
               "breathe", intensity=80, box=(0.2, 0.35, 0.6, 0.3), color="#66FFCC"),
    ]))
    slides.append(make("pulse", [
        _layer("pulse", "static", box=(0.05, 0.05, 0.9, 0.15), color="#FFCC66"),
        _layer("PULSE",
               "pulse", intensity=100, box=(0.2, 0.35, 0.6, 0.3), color="#FF66CC"),
    ]))
    slides.append(make("bounce", [
        _layer("bounce", "static", box=(0.05, 0.05, 0.9, 0.15), color="#FFCC66"),
        _layer("BOUNCE",
               "bounce", intensity=80, box=(0.2, 0.35, 0.6, 0.3), color="#CCFF66"),
    ]))
    slides.append(make("shake", [
        _layer("shake", "static", box=(0.05, 0.05, 0.9, 0.15), color="#FFCC66"),
        _layer("SHAKE",
               "shake", intensity=80, box=(0.2, 0.35, 0.6, 0.3), color="#FF9966"),
    ]))
    slides.append(make("blink", [
        _layer("blink", "static", box=(0.05, 0.05, 0.9, 0.15), color="#FFCC66"),
        _layer("BLINK",
               "blink", intensity=50, box=(0.2, 0.35, 0.6, 0.3), color="#66CCFF"),
    ]))
    slides.append(make("clock", [
        _layer("auto-mode clock", "static",
               box=(0.05, 0.05, 0.9, 0.15), color="#FFCC66"),
        _layer("", "static", auto_mode="time",
               box=(0.1, 0.3, 0.8, 0.4), color="#FFFFFF"),
    ]))
    slides.append(make("multi", [
        _layer("multi: ticker + pulse + clock", "static",
               box=(0.05, 0.05, 0.9, 0.1), color="#FFCC66"),
        _layer("HEADLINE — moving across the top",
               "ticker", intensity=70, box=(0.05, 0.18, 0.9, 0.15), color="#FF6666"),
        _layer("MID-LAYER PULSE",
               "pulse", intensity=80, box=(0.2, 0.45, 0.6, 0.2), color="#66FF66"),
        _layer("", "static", auto_mode="time",
               box=(0.2, 0.75, 0.6, 0.2), color="#FFFFFF"),
    ]))

    return slides


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("phase6-live")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration", type=float, default=120,
        help="seconds to run the playlist loop (default 120)",
    )
    args = parser.parse_args()

    card = Path("/dev/dri/card0")
    if not card.exists():
        print(f"ERR: {card} missing", file=sys.stderr)
        return 1

    slides = _build_slides()
    log.info("built %d motion slides", len(slides))

    def fetch_items():
        return slides

    def read_asset(_id):
        # No image/video assets in this harness — only TextSlides.
        return b""

    with DRMRenderer(
        width=SIGN_W, height=SIGN_H, device_path=card,
        pixel_format="xrgb8888",
        max_animated_planes=MAX_ANIMATED_PLANES,
    ) as renderer:
        log.info(
            "DRM: %dx%d display @ %s, %d animated planes reserved",
            renderer.display_width, renderer.display_height,
            renderer.pixel_format, MAX_ANIMATED_PLANES,
        )
        loop = PlaybackLoop(
            renderer=renderer,
            fetch_items=fetch_items,
            read_asset=read_asset,
        )
        await loop.start()
        log.info("playback loop started — running for %.0fs", args.duration)
        try:
            await asyncio.sleep(args.duration)
        finally:
            await loop.stop()
            log.info("done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
