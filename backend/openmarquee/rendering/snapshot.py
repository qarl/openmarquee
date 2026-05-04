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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from openmarquee.auto_render import _load_background
from openmarquee.motion import render_layer_to_rgba
from openmarquee.rendering.blend import composite_with_blend

if TYPE_CHECKING:
    from openmarquee.content import TextSlide

log = logging.getLogger(__name__)


def _slide_has_auto_layer(slide: "TextSlide") -> bool:
    """True iff any visible layer carries auto_mode (clock/date/day).
    Auto-mode layers re-render text from the current time on every
    call, so their snapshots are NOT stable across calls -- caching
    them would either serve stale clock text or invalidate every
    second. Slides with auto layers skip the cache entirely."""
    for layer in getattr(slide, "text_layers", []):
        if not getattr(layer, "visible", True):
            continue
        if getattr(layer, "auto_mode", None):
            return True
    return False


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
        mode = getattr(layer, "blend", "normal") or "normal"
        if mode == "normal":
            bg.alpha_composite(layer_rgba)
        else:
            bg = composite_with_blend(bg, layer_rgba, mode=mode)
    return bg.tobytes()


def compose_slide_bg_statics_rgba(
    slide: "TextSlide",
    width: int,
    height: int,
    *,
    read_asset: Callable[[UUID], bytes] | None = None,
    now: datetime | None = None,
) -> bytes:
    """Variant of `compose_slide_rgba` that includes only bg + STATIC
    layers (motion="static" AND no auto_mode), skipping every animated
    layer.

    Used as the shader transition's u_from / u_to when keeping the
    outgoing/incoming slide's animated overlays alive on multi-plane
    overlay planes (#206). The shader transitions the flat bg+statics
    portion of the slide; HVS composites the animated overlays on top
    at scanout, so animation keeps moving through the transition
    window instead of freezing.

    Without this, baking animated layers' positions into u_from /
    u_to would double-paint with the live overlay planes (frozen
    shader copy + moving HVS copy of the same text).
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
        motion = getattr(layer, "motion", None) or "static"
        auto = getattr(layer, "auto_mode", None)
        if motion != "static" or auto:
            continue  # animated -- handled by overlay planes during transition
        layer_rgba = render_layer_to_rgba(layer, width, height, now=now)
        mode = getattr(layer, "blend", "normal") or "normal"
        if mode == "normal":
            bg.alpha_composite(layer_rgba)
        else:
            bg = composite_with_blend(bg, layer_rgba, mode=mode)
    return bg.tobytes()


@dataclass
class _CachedSnapshot:
    """One slide's RGBA snapshots, keyed by slide.updated_at. Both
    full-composite (for u_to / static-slide transition input) and
    bg+statics-only (for u_from in the #206 motion-through-transition
    path) are cached lazily on first lookup. Either may be None until
    populated."""

    updated_at: datetime | None
    full_rgba: bytes | None = None
    bg_statics_rgba: bytes | None = None


class SlideSnapshotCache:
    """Cross-slide RGBA snapshot cache for the shader transition path.

    Without a cache, every shader transition pays for one PIL bg load
    + alpha_composite per layer for u_from AND for u_to. On Pi Zero 2 W
    at 1080p that's ~600 ms per slide -- the playback thread stalls
    ~1.2 s before each fade begins (visible as a freeze on the
    outgoing slide). With this cache, the second time through a
    playlist (and every time after) is a few hundred microseconds.

    Keyed by slide.id; entries invalidate when slide.updated_at moves.
    Slides with auto-mode layers (clocks etc.) skip the cache entirely
    -- their text re-renders from the current time, so any cached
    bytes would either serve stale text or invalidate every second.

    Lives on PlaybackLoop (set in __init__, cleared in stop).
    """

    def __init__(self) -> None:
        self._entries: dict[UUID, _CachedSnapshot] = {}

    def _maybe_get(
        self, slide: "TextSlide",
    ) -> _CachedSnapshot | None:
        """Return the cache entry for `slide` IFF it matches the
        slide's current `updated_at`. Returns None when the slide has
        an auto layer (no caching) or the slide has no id; otherwise
        ensures an entry exists (possibly empty) and returns it."""
        if _slide_has_auto_layer(slide):
            return None
        slide_id = getattr(slide, "id", None)
        if slide_id is None:
            return None
        entry = self._entries.get(slide_id)
        slide_updated = getattr(slide, "updated_at", None)
        if entry is None or entry.updated_at != slide_updated:
            entry = _CachedSnapshot(updated_at=slide_updated)
            self._entries[slide_id] = entry
        return entry

    def get_full(
        self,
        slide: "TextSlide",
        width: int,
        height: int,
        *,
        read_asset: Callable[[UUID], bytes] | None = None,
        now: datetime | None = None,
    ) -> bytes:
        """Cached compose_slide_rgba. Populates on first call."""
        entry = self._maybe_get(slide)
        if entry is not None and entry.full_rgba is not None:
            return entry.full_rgba
        rgba = compose_slide_rgba(
            slide, width, height, read_asset=read_asset, now=now,
        )
        if entry is not None:
            entry.full_rgba = rgba
        return rgba

    def get_bg_statics(
        self,
        slide: "TextSlide",
        width: int,
        height: int,
        *,
        read_asset: Callable[[UUID], bytes] | None = None,
        now: datetime | None = None,
    ) -> bytes:
        """Cached compose_slide_bg_statics_rgba. Populates on first
        call."""
        entry = self._maybe_get(slide)
        if entry is not None and entry.bg_statics_rgba is not None:
            return entry.bg_statics_rgba
        rgba = compose_slide_bg_statics_rgba(
            slide, width, height, read_asset=read_asset, now=now,
        )
        if entry is not None:
            entry.bg_statics_rgba = rgba
        return rgba

    def clear(self) -> None:
        """Drop all cached entries. Called on PlaybackLoop.stop() so
        a fresh start() doesn't reuse stale (slide_id, updated_at)
        entries against a content store that may have changed."""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
