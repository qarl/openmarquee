#!/usr/bin/env python3
"""Phase 7 blend-modes smoke -- render a slide for each blend mode.

For each of {normal, multiply, screen, overlay}, builds a TextSlide
with a colored bg + a static text layer that uses that blend mode,
runs compose_slide_rgba, saves the result as PNG.

Visual verification of #198. Doesn't require the dev Pi -- runs
anywhere PIL + numpy work. Outputs land in /tmp/phase7_blend_*.png.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from openmarquee.content import TextBox, TextLayer, TextSlide  # noqa: E402
from openmarquee.rendering.snapshot import compose_slide_rgba  # noqa: E402

W, H = 800, 450  # smaller than 1080p so the smoke runs fast


def _build_slide_with_blend(mode: str) -> TextSlide:
    """Mid-saturation orange bg + a half-canvas mid-gray rectangle on
    top with the given blend mode. Distinct enough that each mode
    produces a visibly different output:

      normal:   gray rectangle covers half the orange, source-over.
      multiply: orange darkens (gray*orange/255 = a darker orange).
      screen:   orange brightens (lighter, washed-out look).
      overlay:  contrast added -- orange's bright areas brighten more,
                dark areas darken more.

    The bg is a solid color (no asset) so the smoke needs no
    read_asset.
    """
    return TextSlide(
        id=uuid4(),
        name=f"blend-test-{mode}",
        background_color="#e87b22",  # mid-saturation orange
        text_layers=[
            TextLayer(
                text=mode.upper(),
                name=f"blend-{mode}",
                font_size_pct=40.0,
                text_color="#888888",  # mid-gray
                box=TextBox(x=0.05, y=0.30, w=0.90, h=0.40),
                anchor="center",
                blend=mode,  # type: ignore[arg-type]
            ),
        ],
    )


def main() -> int:
    for mode in ("normal", "multiply", "screen", "overlay"):
        slide = _build_slide_with_blend(mode)
        rgba = compose_slide_rgba(slide, W, H, read_asset=None)
        img = Image.frombytes("RGBA", (W, H), rgba)
        out = Path(f"/tmp/phase7_blend_{mode}.png")
        img.save(out)
        print(f"wrote {out}: {img.size} (RGBA)")

    print(
        "\nAll four PNGs written. "
        "Inspect to confirm visual differences between modes."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
