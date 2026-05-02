"""GPU-side slide compositor for HDMI 1080p deployments.

Drives a multi-plane DRMRenderer per slide. Background + every static
text layer are software-composited into the primary plane ONCE at slide
entry; each animated text layer (motion != "static" or auto_mode set)
gets its own DRM overlay plane with a glyph-bbox-cropped RGBA bitmap.

Per-tick motion is one atomic ioctl with the changed plane properties
(CRTC_X/Y/W/H, alpha) — zero per-pixel CPU work in the hot path. The
existing motion.py per-effect math (compute_phase, _effect_freq, the
shake seed function, breathe's box-center orbit, etc.) is reused
verbatim — only the OUTPUT changes (DRM property deltas instead of
PIL pixel transforms).

This replaces compose_motion_frame on the device-render path for
HDMI 1080p (qarl 2026-05-02; see project_hdmi_1080p_is_primary_target).
The software path stays for the rare LED-matrix case where the whole
slide rasterizes through the existing render_frame protocol.

Design doc: docs/multi-plane-gpu-compositor.md.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np

from openmarquee.auto_render import _load_background, render_auto_text_for_layer
from openmarquee.motion import (
    _box_px,
    _effect_freq,
    _shake_seed,
    compute_phase,
    render_layer_to_rgba,
)

if TYPE_CHECKING:
    from uuid import UUID

    from openmarquee.content import TextLayer, TextSlide

log = logging.getLogger(__name__)


@runtime_checkable
class MultiPlaneRenderer(Protocol):
    """Capability protocol the GPUSlideCompositor needs from a renderer.
    DRMRenderer satisfies this when constructed with `max_animated_planes
    > 0`. PlaybackLoop step 3 will detect the protocol via `isinstance`
    to decide whether to route through the GPU compositor or the
    software compose_motion_frame fallback. `max_animated_planes` is
    exposed so the loop can fall back to software for slides whose
    animated-layer count exceeds the plane budget."""

    width: int
    height: int
    max_animated_planes: int

    def render_frame(self, frame: bytes) -> None: ...
    def attach_animated_layer(
        self,
        slot_idx: int,
        rgba_bytes: bytes,
        *,
        src_w: int,
        src_h: int,
        crtc_x: int,
        crtc_y: int,
        crtc_w: int,
        crtc_h: int,
        zpos: int | None = None,
    ) -> None: ...
    def update_animated_layer(self, slot_idx: int, **kwargs: object) -> None: ...
    def detach_animated_layer(self, slot_idx: int) -> None: ...
    def commit(self) -> None: ...


def classify_layer(layer: "TextLayer") -> str:
    """Bucket a layer for the GPU compositor: "hidden" / "static" /
    "animated". Static = renders into the primary plane once at attach.
    Animated = gets its own overlay plane (motion or auto_mode triggers
    this — auto layers re-rasterize per tick into their plane buffer)."""
    if not getattr(layer, "visible", True):
        return "hidden"
    motion = getattr(layer, "motion", "static") or "static"
    auto = getattr(layer, "auto_mode", None)
    if motion != "static" or auto:
        return "animated"
    return "static"


class GPUSlideCompositor:
    """Per-slide orchestrator that maps a TextSlide onto a renderer's
    multi-plane DRM API.

    Lifecycle:
        c = GPUSlideCompositor(slide, renderer, width=W, height=H)
        c.attach(now=now)          # slide entry: paint primary, attach planes
        while playing:
            c.tick(elapsed_s, now) # per-frame: stage updates + commit
        c.detach()                 # slide exit: detach all planes

    Plane budget:
        Each animated layer consumes one renderer slot (0..max_animated_
        planes-1). If a slide has more animated layers than the renderer
        was constructed with, attach() raises with a clear message —
        PlaybackLoop is expected to either lower the slide's animated
        count or fall back to the software compose path.
    """

    def __init__(
        self,
        slide: "TextSlide",
        renderer: MultiPlaneRenderer,
        *,
        width: int,
        height: int,
        read_asset: Callable[["UUID"], bytes] | None = None,
    ) -> None:
        self.slide = slide
        self.renderer = renderer
        self.width = width
        self.height = height
        self.read_asset = read_asset

        # layer_idx (in slide.text_layers) → plane slot_idx on the
        # renderer. Only entries for animated layers (motion or
        # auto_mode); static layers are absent (they live in the primary
        # plane). The slot mapping is FIXED FOR THE SLIDE'S LIFETIME,
        # regardless of whether the buffer is currently attached. This
        # is what lets an auto layer go empty mid-slide (e.g. clock
        # format produces "" briefly) and recover on the next rollover
        # via the same slot — without that stability the layer would
        # stay dark permanently after one empty render.
        self._slot_for_layer: dict[int, int] = {}
        # layer_idx → (box_x, box_y, box_w, box_h) in display pixels.
        # Box dims drive ticker sweep distance, breathe pivot, bounce
        # amplitude. Cached at attach so per-tick math is just arithmetic.
        self._box_px: dict[int, tuple[int, int, int, int]] = {}
        # layer_idx → (gx, gy, gw, gh): the glyph bbox WITHIN the slide-
        # sized rasterization. (gx, gy) is the at-rest CRTC origin;
        # (gw, gh) is the at-rest CRTC size and the SRC dims of the
        # plane buffer. Absence of an entry signals "this layer's slot
        # has no live buffer right now — skip per-tick motion staging
        # until a future rasterize produces ink and re-attaches."
        self._glyph_dims: dict[int, tuple[int, int, int, int]] = {}
        # Per-auto-layer cache of last-rendered text. When auto_mode's
        # text changes (clock minute rollover, etc.), we re-rasterize
        # and re-attach to refresh the plane buffer.
        self._auto_text: dict[int, str] = {}
        self._slide_id = str(getattr(slide, "id", "?"))
        self._attached = False

    # --- lifecycle ---

    def attach(self, *, now: datetime | None = None) -> None:
        """Slide entry: classify layers, paint primary plane (bg + every
        static layer software-composited), attach each animated layer
        to its own renderer plane slot, commit once.

        This is a one-time cost per slide entry — typically 10-30 ms at
        1080p (one slide-sized alpha_composite per static layer, one
        glyph-bbox crop per animated layer). Subsequent ticks are free.
        """
        if self._attached:
            raise RuntimeError("GPUSlideCompositor already attached — call detach() first")

        layers = list(getattr(self.slide, "text_layers", []))
        animated: list[tuple[int, "TextLayer"]] = []
        n_static = 0

        # 1. Background + static layers → primary plane (one-time CPU
        # composite). Static = motion is static AND auto_mode unset.
        bg = _load_background(self.slide, self.width, self.height, self.read_asset)
        if bg.mode != "RGBA":
            bg = bg.convert("RGBA")
        for idx, layer in enumerate(layers):
            kind = classify_layer(layer)
            if kind == "hidden":
                continue
            if kind == "static":
                static_rgba = render_layer_to_rgba(layer, self.width, self.height)
                bg.alpha_composite(static_rgba)
                n_static += 1
            else:
                animated.append((idx, layer))

        self.renderer.render_frame(bg.convert("RGB").tobytes())

        # 2. Animated layers → one overlay plane each.
        for slot_idx, (layer_idx, layer) in enumerate(animated):
            try:
                self._attach_animated(slot_idx, layer_idx, layer, now=now)
            except IndexError as e:
                budget = getattr(self.renderer, "max_animated_planes", "?")
                raise RuntimeError(
                    f"slide {self._slide_id}: {len(animated)} animated layers "
                    f"exceed renderer's {budget}-plane budget (slot {slot_idx} "
                    f"out of range). Either lower the slide's animated count "
                    f"or fall back to the software compose path."
                ) from e

        self.renderer.commit()
        self._attached = True

        log.info(
            "GPUSlideCompositor: slide %s attached (%d static, %d animated)",
            self._slide_id, n_static, len(animated),
        )

    def tick(self, elapsed_s: float, now: datetime | None = None) -> None:
        """Per-tick (called at PlaybackLoop's render rate, typically
        30 Hz for motion / 1 Hz for auto-only): refresh any auto-layer
        text whose source changed, then stage motion-driven property
        deltas for each animated layer, then one atomic commit.

        On a motion-only slide, per-tick CPU work is six floats of
        sin/lin per layer + one ioctl. On a slide with an auto layer
        whose text just rolled over (e.g. clock 12:34 → 12:35), one
        layer re-rasterizes (~5-10 ms at 1080p) and the plane buffer
        is re-uploaded. Still well under the 33 ms budget for one
        layer roll-over per tick."""
        if not self._attached:
            raise RuntimeError("GPUSlideCompositor not attached — call attach() first")

        layers = list(getattr(self.slide, "text_layers", []))

        # 1. Refresh auto-layer text where it changed.
        for layer_idx, slot_idx in self._slot_for_layer.items():
            layer = layers[layer_idx]
            if not getattr(layer, "auto_mode", None):
                continue
            if now is None:
                continue
            new_text = render_auto_text_for_layer(layer, now)
            if new_text == self._auto_text.get(layer_idx):
                continue
            # Text rolled over — re-rasterize + re-attach (replaces the
            # plane's buffer + bbox dims; subsequent motion math uses
            # the new glyph dims via _glyph_dims update inside the
            # helper).
            self._attach_animated(slot_idx, layer_idx, layer, now=now)
            self._auto_text[layer_idx] = new_text

        # 2. Per-layer motion property deltas.
        for layer_idx, slot_idx in self._slot_for_layer.items():
            layer = layers[layer_idx]
            motion = getattr(layer, "motion", "static") or "static"
            if motion == "static":
                continue
            self._stage_motion(slot_idx, layer_idx, layer, motion, elapsed_s)

        self.renderer.commit()

    def detach(self) -> None:
        """Slide exit: detach every animated plane (CRTC_ID = 0 / FB_ID =
        0), one commit. The next slide's attach() will paint a new
        primary frame and re-attach planes — there is no flicker if
        the orchestrator overlaps detach + attach within one tick."""
        if not self._attached:
            return
        for slot_idx in self._slot_for_layer.values():
            self.renderer.detach_animated_layer(slot_idx)
        self.renderer.commit()
        self._slot_for_layer.clear()
        self._box_px.clear()
        self._glyph_dims.clear()
        self._auto_text.clear()
        self._attached = False

    # --- internals ---

    def _attach_animated(
        self,
        slot_idx: int,
        layer_idx: int,
        layer: "TextLayer",
        *,
        now: datetime | None,
    ) -> None:
        """Rasterize one layer at slide dims, find its glyph bbox,
        crop, attach to the named plane slot. Used both at slide entry
        and on auto-layer text rollover (re-attach replaces the plane
        buffer).

        Slot reservation is stable for the slide's lifetime — this
        method always sets `_slot_for_layer[layer_idx] = slot_idx` and
        `_box_px[layer_idx]` even when the rasterized text has no ink.
        Without that, an auto layer that ever produces empty text would
        be dropped from the per-tick iteration and never recover when
        text returns. Buffer presence is tracked separately via
        `_glyph_dims`."""
        # Reserve the slot mapping unconditionally — required for the
        # auto-rollover empty-then-nonempty recovery path.
        self._slot_for_layer[layer_idx] = slot_idx
        self._box_px[layer_idx] = _box_px(layer, self.width, self.height)

        layer_rgba = render_layer_to_rgba(layer, self.width, self.height, now=now)
        glyph_bbox = layer_rgba.getbbox()
        if glyph_bbox is None:
            # No ink (empty / whitespace-only / auto formatting yielded
            # ""). Detach the renderer buffer if one was previously
            # attached. Keep the slot mapping alive so the next tick can
            # retry on text rollover; absence of `_glyph_dims[layer_idx]`
            # signals "no buffer; skip per-tick motion staging."
            if layer_idx in self._glyph_dims:
                self.renderer.detach_animated_layer(slot_idx)
                self._glyph_dims.pop(layer_idx)
            log.debug(
                "GPUSlideCompositor: layer %d has no ink; slot %d buffer "
                "detached but mapping retained for rollover recovery",
                layer_idx, slot_idx,
            )
            return

        gx, gy, gx2, gy2 = glyph_bbox
        gw, gh = gx2 - gx, gy2 - gy
        rgba_bytes = layer_rgba.crop(glyph_bbox).tobytes()

        self.renderer.attach_animated_layer(
            slot_idx,
            rgba_bytes,
            src_w=gw, src_h=gh,
            crtc_x=gx, crtc_y=gy,
            crtc_w=gw, crtc_h=gh,
        )
        self._glyph_dims[layer_idx] = (gx, gy, gw, gh)
        if getattr(layer, "auto_mode", None) and now is not None:
            self._auto_text[layer_idx] = render_auto_text_for_layer(layer, now)

    def _stage_motion(
        self,
        slot_idx: int,
        layer_idx: int,
        layer: "TextLayer",
        motion: str,
        elapsed_s: float,
    ) -> None:
        """Compute per-effect motion deltas and stage them via update_
        animated_layer. Math mirrors motion.py's per-effect transforms
        but emits plane-property changes (CRTC_X/Y/W/H, alpha) instead
        of pixel transforms."""
        if layer_idx not in self._glyph_dims:
            # Slot is reserved but has no live buffer (auto layer
            # currently empty). Nothing to animate until next rollover.
            return
        intensity = int(getattr(layer, "motion_intensity", 50))
        motion_phase_offset = float(getattr(layer, "motion_phase", 0.0))
        phase = compute_phase(
            elapsed_s, _effect_freq(motion, intensity), motion_phase_offset
        )
        bx, by, bw, bh = self._box_px[layer_idx]
        gx, gy, gw, gh = self._glyph_dims[layer_idx]

        if motion == "ticker":
            # Sweep glyph leftward across the box. At phase=0, glyph's
            # left edge is at box's right edge (just off-box right).
            # At phase=1, glyph's right edge is at box's left edge
            # (just off-box left). One snap per cycle when phase wraps
            # 1 → 0.
            #
            # KNOWN BEHAVIOR DIVERGENCE from software ticker (motion.py:
            # _apply_ticker): software wraps via np.roll, so text leaving
            # the left re-enters from the right with no snap — a
            # continuous marquee. The GPU equivalent (snap-then-restart)
            # is visually different. A future iteration could pre-render
            # text twice into a 2*box_w-wide source bitmap and slide
            # SRC_X to do a true wrap, but that needs an attach-time
            # buffer-vs-src dim split in the DRM API. Deferred until
            # operators flag the snap as a problem.
            sweep_total = bw + gw
            crtc_x = bx + bw - int(round(phase * sweep_total))
            self.renderer.update_animated_layer(slot_idx, crtc_x=crtc_x)
            return

        if motion == "breathe":
            # Sine scale around the BOX center (not glyph center) so
            # operator-placed offset is preserved through the cycle.
            # Mirrors motion._apply_breathe's pivot rule.
            amplitude = (intensity / 100.0) * 0.20
            s = 1.0 + amplitude * math.sin(2 * math.pi * phase)
            new_w = max(1, int(round(gw * s)))
            new_h = max(1, int(round(gh * s)))
            box_cx = bx + bw / 2
            box_cy = by + bh / 2
            glyph_cx = gx + gw / 2
            glyph_cy = gy + gh / 2
            new_cx = box_cx + s * (glyph_cx - box_cx)
            new_cy = box_cy + s * (glyph_cy - box_cy)
            self.renderer.update_animated_layer(
                slot_idx,
                crtc_x=int(round(new_cx - new_w / 2)),
                crtc_y=int(round(new_cy - new_h / 2)),
                crtc_w=new_w,
                crtc_h=new_h,
            )
            return

        if motion == "pulse":
            # Sine alpha modulation. min_a controlled by intensity:
            # at 0 the layer barely breathes, at 100 it dips to 0.
            # Plane.alpha is 0..65535; we map directly.
            min_a = 1.0 - intensity / 100.0
            s = (math.sin(2 * math.pi * phase) + 1) / 2
            a = min_a + (1.0 - min_a) * s
            alpha = max(0, min(65535, int(round(a * 65535))))
            self.renderer.update_animated_layer(slot_idx, alpha=alpha)
            return

        if motion == "bounce":
            # Sine vertical bob. CRTC_Y offsets from at-rest gy.
            # No wrap (clipped at edges by HVS naturally).
            amplitude = (intensity / 100.0) * 0.10
            offset_px = int(round(amplitude * bh * math.sin(2 * math.pi * phase)))
            self.renderer.update_animated_layer(slot_idx, crtc_y=gy + offset_px)
            return

        if motion == "shake":
            # Deterministic Gaussian micro-jitter. Same seed shape as
            # motion._apply_shake (slide_id:layer_idx + motion_phase
            # + step) so device + editor preview produce identical
            # offset sequences.
            if intensity <= 0:
                return
            amplitude_px = max(1, int(round((intensity / 100.0) * 0.04 * gh)))
            step = int(phase * 1000) // 100
            layer_key = f"{self._slide_id}:{layer_idx}"
            seed = _shake_seed(layer_key, motion_phase_offset, step)
            rng = np.random.default_rng(seed)
            dx = int(round(rng.normal(0, amplitude_px / 2)))
            dy = int(round(rng.normal(0, amplitude_px / 2)))
            self.renderer.update_animated_layer(
                slot_idx, crtc_x=gx + dx, crtc_y=gy + dy,
            )
            return

        if motion == "blink":
            # Square-wave on/off. 50% duty: ON for first half of cycle.
            alpha = 65535 if phase < 0.5 else 0
            self.renderer.update_animated_layer(slot_idx, alpha=alpha)
            return

        # Unknown motion name: leave at attach-time at-rest geometry.
        log.debug(
            "GPUSlideCompositor: unknown motion %r on layer %d, no per-tick update",
            motion, layer_idx,
        )
