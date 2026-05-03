"""PlaybackLoop integration with the GPU compositor (step 3).

Verifies the path-selection logic in `_play_dynamic_slide`:

- a TextSlide with motion + a multi-plane-capable renderer + sufficient
  plane budget → routes through GPUSlideCompositor (attach/tick/detach)
- otherwise → routes through the software compose_motion_frame path

Per-effect math + compositor lifecycle correctness live in
`test_gpu_compositor.py`. This file checks the WIRING only — that the
right path runs in the right conditions and the GPU lifecycle hooks
fire when it does.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from PIL import Image

from openmarquee.content import TextBox, TextLayer, TextSlide
from openmarquee.playback import PlaybackLoop, _count_animated_layers
from openmarquee.rendering.mock import MockRenderer

_FAST_DURATION_MS = 100
_FAST_EMPTY_POLL = 0.01


# --- multi-plane test renderer ---


class FakeMultiPlaneRendererWithPNG:
    """Satisfies MultiPlaneRenderer protocol AND PlaybackLoop's
    expectation that the renderer has width/height + render_frame.
    Records every multi-plane API call so tests can assert the GPU
    path drove the lifecycle, plus the primary frame bytes so static-
    layer composite is verifiable."""

    def __init__(self, width: int, height: int, *, max_animated_planes: int = 4):
        self.width = width
        self.height = height
        self.max_animated_planes = max_animated_planes
        self.last_frame: bytes | None = None
        self.attach_calls: list[int] = []  # slot_idx per attach
        self.update_calls: list[tuple[int, dict]] = []  # (slot_idx, kwargs)
        self.detach_calls: list[int] = []  # slot_idx per detach
        self.commits: int = 0

    def render_frame(self, frame: bytes) -> None:
        self.last_frame = frame

    def attach_animated_layer(
        self, slot_idx: int, rgba_bytes: bytes, **kwargs: object
    ) -> None:
        if slot_idx < 0 or slot_idx >= self.max_animated_planes:
            raise IndexError(
                f"animated slot {slot_idx} out of range "
                f"[0..{self.max_animated_planes - 1}]"
            )
        self.attach_calls.append(slot_idx)

    def update_animated_layer(self, slot_idx: int, **kwargs: object) -> None:
        self.update_calls.append((slot_idx, dict(kwargs)))

    def detach_animated_layer(self, slot_idx: int) -> None:
        self.detach_calls.append(slot_idx)

    def commits_so_far(self) -> int:
        return self.commits

    def commit(self) -> None:
        self.commits += 1


# --- helpers ---


def _solid_text_slide(
    name: str,
    *,
    motion: str = "static",
    auto_mode: str | None = None,
    duration_ms: int = _FAST_DURATION_MS,
    layers: int = 1,
) -> TextSlide:
    """Single- or multi-layer TextSlide. All layers share the same
    motion / auto_mode for budget tests."""
    text_layers = [
        TextLayer(
            text=f"L{i}",
            motion=motion,
            auto_mode=auto_mode,
            box=TextBox(x=0.1, y=0.1, w=0.8, h=0.8),
        )
        for i in range(layers)
    ]
    return TextSlide(
        name=name,
        text_layers=text_layers,
        duration_ms=duration_ms,
        background_color="#000033",
    )


def _new_loop(renderer, *, items, **kwargs):
    return PlaybackLoop(
        renderer,
        fetch_items=lambda: items,
        read_asset=lambda _id: b"",
        empty_playlist_poll_seconds=_FAST_EMPTY_POLL,
        auto_tick_seconds=0.02,
        **kwargs,
    )


# --- _count_animated_layers ---


def test_count_animated_layers_static_text():
    slide = _solid_text_slide("s", motion="static")
    assert _count_animated_layers(slide) == 0


def test_count_animated_layers_motion_text():
    slide = _solid_text_slide("s", motion="ticker", layers=2)
    assert _count_animated_layers(slide) == 2


def test_count_animated_layers_auto_text():
    slide = _solid_text_slide("s", motion="static", auto_mode="time")
    assert _count_animated_layers(slide) == 1


def test_count_animated_layers_skips_hidden():
    slide = TextSlide(
        name="s",
        text_layers=[
            TextLayer(text="A", motion="ticker", visible=True,
                      box=TextBox(x=0.1, y=0.1, w=0.8, h=0.8)),
            TextLayer(text="B", motion="ticker", visible=False,
                      box=TextBox(x=0.1, y=0.1, w=0.8, h=0.8)),
        ],
    )
    assert _count_animated_layers(slide) == 1


def test_count_animated_layers_image_slide_returns_zero():
    """Non-text-slide content shouldn't trigger the GPU path at all."""
    class FakeImageItem:
        type = "image_slide"
    assert _count_animated_layers(FakeImageItem()) == 0


# --- path selection: GPU vs software ---


@pytest.mark.asyncio
async def test_motion_slide_routes_through_gpu_path(tmp_path):
    """A TextSlide with motion + multi-plane renderer + sufficient
    budget should drive the GPUSlideCompositor lifecycle (attach,
    at least one update, detach)."""
    slide = _solid_text_slide("m", motion="pulse", duration_ms=120)
    renderer = FakeMultiPlaneRendererWithPNG(64, 48)
    loop = _new_loop(renderer, items=[slide])
    await loop.start()
    await asyncio.sleep(0.20)  # spans the slide's 120ms duration
    await loop.stop()

    # Single-item playlist loops, so during the test wait window the
    # slide may play more than once — each cycle attaches+detaches the
    # animated plane. Assert on shape, not exact count.
    assert renderer.attach_calls, "GPU path should attach at least one animated plane"
    assert all(s == 0 for s in renderer.attach_calls), (
        "Pulse layer should always claim slot 0"
    )
    assert any(slot == 0 and "alpha" in kw for slot, kw in renderer.update_calls), (
        "GPU path should stage at least one alpha update for the pulse layer"
    )
    assert renderer.detach_calls, (
        "GPU path must detach the animated plane at slide exit"
    )
    assert all(s == 0 for s in renderer.detach_calls)


@pytest.mark.asyncio
async def test_motion_slide_falls_back_to_software_when_renderer_lacks_gpu(tmp_path):
    """MockRenderer doesn't satisfy MultiPlaneRenderer (no attach_
    animated_layer etc.), so motion slides must use the software path.
    No GPU lifecycle methods should be invoked — and we know they
    can't be, because MockRenderer doesn't have them. If the wiring
    ever forgets the isinstance check and tries to call them, the
    test crashes on AttributeError."""
    slide = _solid_text_slide("m", motion="pulse", duration_ms=120)
    out_path = tmp_path / "out.png"
    renderer = MockRenderer(64, 48, out_path)
    loop = _new_loop(renderer, items=[slide])
    await loop.start()
    await asyncio.sleep(0.20)
    await loop.stop()

    # Software path wrote at least one frame to the mock.
    assert renderer.last_frame is not None
    assert len(renderer.last_frame) == 64 * 48 * 3


@pytest.mark.asyncio
async def test_motion_slide_falls_back_when_budget_exceeded(tmp_path):
    """A slide with more animated layers than the renderer's plane
    budget must fall back to the software path rather than failing."""
    # 3 motion layers, but renderer budget is only 2 → fallback.
    slide = _solid_text_slide("m", motion="pulse", layers=3, duration_ms=120)
    renderer = FakeMultiPlaneRendererWithPNG(64, 48, max_animated_planes=2)
    loop = _new_loop(renderer, items=[slide])
    await loop.start()
    await asyncio.sleep(0.20)
    await loop.stop()

    # GPU path was NOT taken. attach_calls stays empty.
    assert renderer.attach_calls == []
    # Software path still rendered frames via render_frame.
    assert renderer.last_frame is not None


@pytest.mark.asyncio
async def test_zero_budget_renderer_routes_to_software(tmp_path):
    """A multi-plane-capable renderer constructed with budget=0 (the
    default) must route motion slides to the software path. This is
    the existing-deployment compatibility case: today's DRMRenderer
    instances have max_animated_planes=0 and must continue to work
    as before."""
    slide = _solid_text_slide("m", motion="ticker", duration_ms=120)
    renderer = FakeMultiPlaneRendererWithPNG(64, 48, max_animated_planes=0)
    loop = _new_loop(renderer, items=[slide])
    await loop.start()
    await asyncio.sleep(0.20)
    await loop.stop()

    assert renderer.attach_calls == []
    assert renderer.last_frame is not None


@pytest.mark.asyncio
async def test_gpu_attach_partial_failure_cleans_up_then_falls_back(tmp_path):
    """If GPUSlideCompositor.attach raises mid-way (after some planes
    were bound but before all were), the cleanup path must detach the
    successfully-bound planes before re-raising. The playback loop's
    outer try/except then falls through to the software compose path —
    on a renderer with NO leftover overlay planes attached. Without
    this cleanup, ghost text from the abandoned slide would composite
    over every subsequent software-rendered frame."""
    class _SecondAttachRaisesRenderer(FakeMultiPlaneRendererWithPNG):
        def attach_animated_layer(self, slot_idx, rgba_bytes, **kwargs):
            if slot_idx == 1:
                # First plane attached cleanly; second one fails. The
                # cleanup must roll back the first.
                raise RuntimeError("simulated transient kernel error")
            super().attach_animated_layer(slot_idx, rgba_bytes, **kwargs)

    # Slide has 2 animated layers. First attach succeeds (slot 0 bound),
    # second attach raises (slot 1).
    slide = _solid_text_slide("m", motion="pulse", layers=2, duration_ms=120)
    renderer = _SecondAttachRaisesRenderer(64, 48)
    loop = _new_loop(renderer, items=[slide])
    await loop.start()
    await asyncio.sleep(0.20)
    await loop.stop()

    # The first plane was attached; the cleanup must detach it before
    # the software fallback ran.
    assert 0 in renderer.attach_calls, "first plane was attempted"
    assert 0 in renderer.detach_calls, (
        "cleanup must detach successfully-bound plane 0 before the "
        "software fallback paints over an otherwise-clean primary"
    )
    # Software fallback then rendered frames via render_frame.
    assert renderer.last_frame is not None


@pytest.mark.asyncio
async def test_gpu_path_renders_primary_for_transition_handoff(tmp_path):
    """At slide exit on the GPU path, _play_dynamic_slide_gpu computes
    one final compose_motion_frame and paints it to the primary plane
    so the next slide's transition has a clean from-frame. Verify the
    renderer's last_frame is populated by slide exit."""
    slide = _solid_text_slide("m", motion="pulse", duration_ms=120)
    renderer = FakeMultiPlaneRendererWithPNG(64, 48)
    loop = _new_loop(renderer, items=[slide])
    await loop.start()
    await asyncio.sleep(0.20)
    await loop.stop()

    # last_frame is set by render_frame, which the GPU path calls at
    # both attach (bg+static composite) and slide exit (final compose
    # for transition handoff). Either way it's non-None — but the
    # final-compose call ensures a fresh paint at slide exit.
    assert renderer.last_frame is not None
    assert len(renderer.last_frame) == 64 * 48 * 3


@pytest.mark.asyncio
async def test_auto_layer_routes_through_gpu_path(tmp_path):
    """An auto-mode layer (clock/date/day) classifies as animated even
    with motion=static — it should claim a plane and re-rasterize
    per tick into that plane's buffer."""
    slide = _solid_text_slide(
        "a", motion="static", auto_mode="time", duration_ms=120,
    )
    renderer = FakeMultiPlaneRendererWithPNG(64, 48)
    loop = _new_loop(renderer, items=[slide])
    await loop.start()
    await asyncio.sleep(0.20)
    await loop.stop()

    # Auto layer → animated → claims slot 0. Slide loops during the
    # test window, so assert shape not exact count.
    assert renderer.attach_calls
    assert all(s == 0 for s in renderer.attach_calls)
    assert renderer.detach_calls
    assert all(s == 0 for s in renderer.detach_calls)
