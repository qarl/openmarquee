"""Compose a TextSlide into a single RGBA bitmap for shader-transition input.

The shader compositor path needs each slide as a single RGBA texture
(bg + every visible layer alpha-composited). Used as `u_from` /
`u_to` inputs to ShaderRenderer when a transition runs.

The multi-plane DRM compositor pays this same cost at slide entry to
paint its primary plane (gpu_compositor.py:282-289), but it converts
to RGB and discards alpha. This module preserves alpha so the shader
can sample with the original layer transparency intact (matters for
transitions where the OUTGOING slide's transparent regions reveal
whatever the kernel leaves below — typically just a black-clear
underneath).

Animated layers render in their default position. Freezing animation
during the transition window is acceptable — transitions are short
(typically 200-1000 ms) and a frozen-pose snapshot reads cleanly to
the eye, especially for transition kinds that cross-mix or wipe.

For "snapshot at moment X" semantics that capture mid-motion state,
we'd need to apply motion.compute_phase per layer before render.
That's a follow-up; for the current transition kinds (fade, wipe,
iris, dissolve, ...) the per-layer motion freeze isn't visible
because the transition itself dominates the perceived motion.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from openmarquee.auto_render import _load_background
from openmarquee.motion import render_layer_to_rgba

if TYPE_CHECKING:
    from openmarquee.content import TextSlide

log = logging.getLogger(__name__)


def compose_slide_rgba(
    slide: "TextSlide",
    width: int,
    height: int,
    *,
    read_asset: Callable[[UUID], bytes] | None = None,
    now: datetime | None = None,
) -> bytes:
    """Return the slide's full composite as a width*height*4 RGBA byte
    buffer in row-major top-down order.

    Bg + every visible layer alpha-composited in array order (later
    entries draw on top). Hidden layers (visible=False) are skipped.
    Auto-mode layers (clock / date / day-of-week) rerasterize against
    `now`, so a clock slide captures the actual current time in its
    snapshot. `now` defaults to UTC-now — the alternative (None) makes
    motion.render_layer_to_rgba fall through to layer.text, which is
    typically empty for auto layers and produces a blank-clock
    snapshot. Caller can override `now` to back-date for testing.

    NOTE: this differs from gpu_compositor's slide-entry composite,
    which only flattens *static* layers into the primary plane — it
    keeps animated layers on overlay planes for HVS scanout. The
    snapshot here flattens EVERYTHING including animated layers in
    their default (un-transformed) pose. Right call for a transition
    snapshot — frozen pose during the transition window reads cleanly
    to the eye and short-circuits the bandwidth + plane-budget cost
    of multi-plane during transitions. Don't try to reuse the multi-
    plane primary-plane bytes here — the byte content differs.

    Currently TextSlide-only. ImageSlide / VideoSlide need a separate
    snapshot path.
    """
    if now is None:
        now = datetime.now(UTC)
    if (
        getattr(slide, "background_image_slide_id", None) is not None
        and read_asset is None
    ):
        log.warning(
            "snapshot: slide %s has background_image_slide_id but no "
            "read_asset; bg falls through to pattern/solid",
            getattr(slide, "id", "<no-id>"),
        )

    bg = _load_background(slide, width, height, read_asset)
    if bg.mode != "RGBA":
        bg = bg.convert("RGBA")
    for layer in getattr(slide, "text_layers", []):
        if not getattr(layer, "visible", True):
            continue
        layer_rgba = render_layer_to_rgba(layer, width, height, now=now)
        bg.alpha_composite(layer_rgba)
    return bg.tobytes()
