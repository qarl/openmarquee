"""Re-render every saved TextSlide's PNG at fresh display dims.

When the operator changes display_rotation / display_width / display_height
in /api/settings, the existing text-slide PNGs stay frozen at their
original-render dims. The frontend ends up letterboxing/squishing them
into the new aspect, which looks wrong — long text that should now squish
horizontally just renders smaller after letterbox crop.

This module's `rerender_text_slides_for_dims(...)` walks the content store,
re-renders every text-only or text+image-bg slide via
`render_text_slide_png` (the same squish-aware path seed.py uses), and
saves the new PNG back. It's meant to run as a FastAPI BackgroundTasks
side-effect of the settings PUT — the operator's HTTP response returns
fast, the actual re-render happens after the response is flushed.

Skipped:
- text+video-bg slides: the device composites the text PNG over each
  video frame at playback time per SYSTEM_SPEC §5.10, so the saved PNG
  is just a static thumbnail. The editor regenerates it at the next
  save anyway.
- non-text slides (image, video): unaffected by display dims.
"""

from __future__ import annotations

import logging

from openmarquee.content import TextSlide
from openmarquee.content.storage import ContentStorage
from openmarquee.seed import render_text_slide_png

log = logging.getLogger(__name__)


def effective_dims(rotation: int, width: int, height: int) -> tuple[int, int]:
    """Apply rotation to landscape-native dims. 90/270 → swap; 0/180 → through."""
    if int(rotation) in (90, 270):
        return (int(height), int(width))
    return (int(width), int(height))


def rerender_text_slides_for_dims(
    storage: ContentStorage,
    rotation: int,
    width: int,
    height: int,
) -> int:
    """Re-render every text slide at the rotation-applied dims.

    Returns the count of slides re-rendered. Failures on a single slide
    are logged but don't abort the loop — partial progress is better
    than none, and the next settings PUT or operator edit will heal a
    skipped slide.
    """
    eff_w, eff_h = effective_dims(rotation, width, height)
    rerendered = 0
    items = storage.list_all()

    # Pre-resolve background image asset paths so the PIL composite has
    # a real file to read. ImageSlide.id → asset path.
    bg_paths: dict = {}
    for it in items:
        if it.type == "image":
            bg_paths[it.id] = storage.asset_path(it.id)

    for it in items:
        if not isinstance(it, TextSlide):
            continue
        if it.background_video_slide_id is not None:
            # Text+video-bg: skipped per §5.10 — playback re-composites
            # live, the stored PNG is a static fallback. Operator edits
            # regenerate it client-side.
            continue
        bg_path = (
            bg_paths.get(it.background_image_slide_id)
            if it.background_image_slide_id is not None
            else None
        )
        try:
            png = render_text_slide_png(
                it.text,
                eff_w,
                eff_h,
                fg=it.text_color,
                bg=it.background_color,
                background_image_path=bg_path,
                font_family=it.font_family,
            )
            storage.save_text_slide(it, png)
            rerendered += 1
        except Exception:
            log.exception("text-rerender: %s failed", it.id)
    return rerendered
