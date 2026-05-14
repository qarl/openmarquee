"""Slice 4: playback.py's Rust IPC route.

Asserts that when the injected renderer exposes the Rust IPC ops
(begin_slide + advance), PlaybackLoop drives slides through the
IPC contract instead of the PIL hot path. Three tests:

1. TextSlide reel: N frames render via Rust path (begin_slide + N
   advance ops) with NO compose_motion_frame invocation.

2. VideoSlide: begin_slide raises RustRendererUnsupportedSlideError;
   AutoFallbackRenderer logs + propagates; playback skips the slide
   without crashing.

3. Renderer-detection: a renderer without begin_slide/advance stays
   on the existing PIL path (regression guard for the dispatch gate).
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

from openmarquee.content import TextLayer, TextSlide
from openmarquee.playback import PlaybackLoop
from openmarquee.rendering.mock import MockRenderer
from openmarquee.rendering.rust_renderer import (
    Idle,
    PaintSlide,
    RustRendererUnsupportedSlideError,
    SlideComplete,
)


_FAST_DURATION_MS = 100  # model minimum; keeps test runtime sub-second


def _text_slide(name: str = "x", text: str = "x", **kwargs) -> TextSlide:
    layer_keys = {
        "text_color", "font_family", "font_size_px",
        "font_size_pct", "auto_mode", "auto_format", "box",
    }
    layer = {"text": text}
    for k in list(kwargs.keys()):
        if k in layer_keys:
            layer[k] = kwargs.pop(k)
    return TextSlide(
        name=name,
        text_layers=[TextLayer(**layer)],
        **kwargs,
    )


class _FakeRustRenderer:
    """Minimal IPC-shaped renderer stub. Behaves enough like a real
    proxy for PlaybackLoop's dispatch gate to detect it (begin_slide
    + advance attrs) and drive it for the slide's duration.

    `unsupported_slide_ids` rejects matching slide ids at begin_slide
    with RustRendererUnsupportedSlideError -- the exact failure mode
    a VideoSlide produces today on the sidecar.
    """

    def __init__(
        self,
        width: int = 8,
        height: int = 8,
        *,
        unsupported_slide_ids: set[UUID] | None = None,
    ):
        self.width = width
        self.height = height
        self.unsupported_slide_ids: set[UUID] = unsupported_slide_ids or set()
        self.begin_slide_calls: list[tuple[UUID, int, int]] = []
        self.advance_calls: list[int] = []
        self.render_frame_calls: int = 0
        self._current_slide: UUID | None = None
        self._begin_t_ms: int = 0
        self._duration_ms: int = 0

    # Renderer Protocol surface --------------------------------------

    def render_frame(self, frame: bytes) -> None:
        # Should NOT be reached on the rust route -- the dispatch
        # gate routes around it. Counts so tests can assert zero.
        self.render_frame_calls += 1

    # IPC ops --------------------------------------------------------

    def begin_slide(
        self, slide_id: UUID, t0_ms: int, duration_ms: int
    ) -> None:
        # Record the attempt BEFORE the unsupported-slide rail so tests
        # asserting playback dispatch order see the rejected slide too.
        # Mirrors the real proxy's behavior at the IPC boundary: the
        # send happens before the response comes back as Err.
        self.begin_slide_calls.append((slide_id, t0_ms, duration_ms))
        if slide_id in self.unsupported_slide_ids:
            raise RustRendererUnsupportedSlideError(
                "paint_slide: video slides TBD (image + text both supported)"
            )
        self._current_slide = slide_id
        self._begin_t_ms = int(t0_ms)
        self._duration_ms = int(duration_ms)

    def advance(self, t_ms: int) -> Any:
        self.advance_calls.append(t_ms)
        if self._current_slide is None:
            return Idle()
        elapsed_in_slide = t_ms - self._begin_t_ms
        if elapsed_in_slide >= self._duration_ms:
            return SlideComplete(slide_id=self._current_slide)
        return PaintSlide(
            slide_id=self._current_slide,
            t_in_slide_ms=elapsed_in_slide,
        )


# ============================================================
# Test 1: TextSlide reel renders via Rust path (no PIL).
# ============================================================


@pytest.mark.asyncio
async def test_text_slide_reel_renders_via_rust_route_no_pil():
    """Inject a TextSlide reel + a Rust-shaped renderer; assert the
    loop drives begin_slide + advance for each slide AND never
    invokes compose_motion_frame (the PIL hot path).

    Pins the slice-4 cutover invariant: when the IPC ops are present
    on the renderer, PIL rasterization is skipped entirely. PIL
    imports stay in the module (image fallbacks, transitions) but no
    INVOCATION on this hot path.
    """
    fake = _FakeRustRenderer(width=8, height=8)
    slides = [
        _text_slide(name="A", text="A", duration_ms=_FAST_DURATION_MS),
        _text_slide(name="B", text="B", duration_ms=_FAST_DURATION_MS),
        _text_slide(name="C", text="C", duration_ms=_FAST_DURATION_MS),
    ]

    loop = PlaybackLoop(
        fake,
        fetch_items=lambda: slides,
        read_asset=lambda _id: b"",
        empty_playlist_poll_seconds=0.01,
        auto_tick_seconds=0.02,
    )

    # Patch the PIL-path entry points at the playback module level.
    # If either is called, the rust route was bypassed -- test fails.
    with patch(
        "openmarquee.playback.compose_motion_frame"
    ) as mock_compose, patch.object(
        loop, "_safe_load_image"
    ) as mock_load:
        mock_compose.side_effect = AssertionError(
            "compose_motion_frame called -- rust route bypassed"
        )
        mock_load.side_effect = AssertionError(
            "_safe_load_image called -- rust route bypassed"
        )
        await loop.start()
        # Let the loop chew through the 3-slide reel + start a 4th
        # iteration. 3 slides * 100ms = 300ms minimum.
        await asyncio.sleep(0.35)
        await loop.stop()

    # Three begin_slide calls (one per slide in playlist order).
    assert len(fake.begin_slide_calls) >= 3, (
        f"expected >=3 begin_slide calls, got {len(fake.begin_slide_calls)}"
    )
    seen_slide_ids = [c[0] for c in fake.begin_slide_calls[:3]]
    assert seen_slide_ids == [s.id for s in slides], (
        "begin_slide order doesn't match playlist order"
    )
    # Each slide had multiple advance calls (the per-tick loop).
    assert len(fake.advance_calls) > 3
    # render_frame must NOT have been reached.
    assert fake.render_frame_calls == 0, (
        "render_frame called -- the IPC dispatch gate didn't route around it"
    )


# ============================================================
# Test 2: VideoSlide is skipped on the rust route.
# ============================================================


@pytest.mark.asyncio
async def test_video_slide_unsupported_logs_and_advances(caplog):
    """A VideoSlide raises RustRendererUnsupportedSlideError at
    begin_slide. The playback loop catches via _play_via_rust_ipc
    (which logs at INFO) and advances to the next slide WITHOUT
    crashing or swapping to MockRenderer.
    """
    import logging

    # Two text slides flanking one "video" slide. We simulate the
    # video by configuring the fake to reject its slide_id at
    # begin_slide. The actual slide type doesn't matter to the
    # playback gate -- only the exception path matters here.
    text_a = _text_slide(name="A", text="A", duration_ms=_FAST_DURATION_MS)
    video_like = _text_slide(name="V", text="V", duration_ms=_FAST_DURATION_MS)
    text_b = _text_slide(name="B", text="B", duration_ms=_FAST_DURATION_MS)

    fake = _FakeRustRenderer(
        width=8, height=8,
        unsupported_slide_ids={video_like.id},
    )

    loop = PlaybackLoop(
        fake,
        fetch_items=lambda: [text_a, video_like, text_b],
        read_asset=lambda _id: b"",
        empty_playlist_poll_seconds=0.01,
        auto_tick_seconds=0.02,
    )

    with caplog.at_level(logging.INFO, logger="openmarquee.playback"):
        await loop.start()
        await asyncio.sleep(0.35)
        await loop.stop()

    # All three slides reached begin_slide (the video too -- the
    # exception is raised AT begin_slide, not before).
    slide_ids_begun = [c[0] for c in fake.begin_slide_calls[:3]]
    assert slide_ids_begun == [text_a.id, video_like.id, text_b.id]

    # The skip log line names the unsupported slide id.
    skip_logs = [
        r for r in caplog.records
        if "skipping slide" in r.getMessage() and str(video_like.id) in r.getMessage()
    ]
    assert skip_logs, (
        f"expected skip log for {video_like.id}; got "
        f"{[r.getMessage() for r in caplog.records]}"
    )

    # Loop is still running after the unsupported slide -- no crash.
    # (We stopped it explicitly above; the fact we reached this
    # assertion AND begin_slide_calls includes text_b proves no
    # mid-reel crash.)
    assert len(fake.begin_slide_calls) >= 3


# ============================================================
# Test 3: Non-IPC renderer keeps the existing PIL path.
# ============================================================


def test_non_ipc_renderer_dispatch_gate_evaluates_false(tmp_path):
    """Regression guard: when the renderer doesn't expose begin_slide
    + advance, the dispatch gate evaluates False so the loop stays
    on the existing PIL path. MockRenderer is the canonical non-IPC
    renderer (its Protocol surface is just width/height/render_frame).
    """
    mock = MockRenderer(8, 8, tmp_path / "out.png")
    loop = PlaybackLoop(
        mock,
        fetch_items=lambda: [],
        read_asset=lambda _id: b"",
        empty_playlist_poll_seconds=0.01,
    )
    # The gate is the only thing that decides which route runs.
    assert loop._renderer_supports_ipc_ops() is False
    # MockRenderer really doesn't have these attrs (sanity).
    assert not hasattr(mock, "begin_slide")
    assert not hasattr(mock, "advance")


def test_ipc_renderer_dispatch_gate_evaluates_true():
    """Mirror of the False case: the fake renderer has begin_slide +
    advance, so the gate routes through the rust path. Together with
    the above test, pins the duck-type contract."""
    fake = _FakeRustRenderer(width=8, height=8)
    loop = PlaybackLoop(
        fake,
        fetch_items=lambda: [],
        read_asset=lambda _id: b"",
        empty_playlist_poll_seconds=0.01,
    )
    assert loop._renderer_supports_ipc_ops() is True
