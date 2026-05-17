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
    PaintTransition,
    RustRendererSubprocessError,
    RustRendererUnsupportedSlideError,
    RustRendererUnsupportedTransitionError,
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
        unsupported_transition_kinds: set[str] | None = None,
        subprocess_error_on_begin_transition: bool = False,
    ):
        self.width = width
        self.height = height
        self.unsupported_slide_ids: set[UUID] = unsupported_slide_ids or set()
        self.unsupported_transition_kinds: set[str] = (
            unsupported_transition_kinds or set()
        )
        self.subprocess_error_on_begin_transition = (
            subprocess_error_on_begin_transition
        )
        self.begin_slide_calls: list[tuple[UUID, int, int]] = []
        # (to_slide_id, to_duration_ms, kind, transition_ms, t0_ms)
        self.begin_transition_calls: list[tuple[UUID, int, str, int, int]] = []
        self.advance_calls: list[int] = []
        self.render_frame_calls: int = 0
        self._current_slide: UUID | None = None
        self._begin_t_ms: int = 0
        self._duration_ms: int = 0
        # Transition state (populated by begin_transition; cleared
        # after promote-to-slide on advance).
        self._transition_from: UUID | None = None
        self._transition_to: UUID | None = None
        self._transition_kind: str | None = None
        self._transition_t0_ms: int = 0
        self._transition_ms: int = 0

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
                "Capture: VideoSlide capture not implemented (image + text only)"
            )
        self._current_slide = slide_id
        self._begin_t_ms = int(t0_ms)
        self._duration_ms = int(duration_ms)

    def advance(self, t_ms: int) -> Any:
        self.advance_calls.append(t_ms)
        # Mirror the real Rust state machine (playback.rs::advance):
        # transition state takes precedence during its blend window;
        # when elapsed >= transition_ms, promote to PaintSlide(to_slide)
        # and clear the transition.
        if self._transition_to is not None:
            elapsed = t_ms - self._transition_t0_ms
            if elapsed >= self._transition_ms:
                # Promote to-slide. Set up steady state so subsequent
                # advance() calls return PaintSlide(to_slide).
                self._current_slide = self._transition_to
                self._begin_t_ms = self._transition_t0_ms + self._transition_ms
                # duration_ms stays at whatever the to-slide's was;
                # caller's begin_transition recorded it. We approximate
                # by re-using the existing _duration_ms (or 0); the
                # outer loop will begin_slide-reset shortly after.
                self._transition_from = None
                self._transition_to = None
                self._transition_kind = None
                return PaintSlide(
                    slide_id=self._current_slide,
                    t_in_slide_ms=0,
                )
            return PaintTransition(
                from_id=self._transition_from,
                to=self._transition_to,
                kind=self._transition_kind or "cut",
                progress=elapsed / max(1, self._transition_ms),
            )
        if self._current_slide is None:
            return Idle()
        elapsed_in_slide = t_ms - self._begin_t_ms
        if elapsed_in_slide >= self._duration_ms:
            return SlideComplete(slide_id=self._current_slide)
        return PaintSlide(
            slide_id=self._current_slide,
            t_in_slide_ms=elapsed_in_slide,
        )

    def begin_transition(
        self,
        to_slide_id: UUID,
        to_duration_ms: int,
        kind: str,
        transition_ms: int,
        t0_ms: int,
    ) -> None:
        self.begin_transition_calls.append(
            (to_slide_id, to_duration_ms, kind, transition_ms, t0_ms)
        )
        if self.subprocess_error_on_begin_transition:
            raise RustRendererSubprocessError(
                "subprocess died during op 'begin_transition'"
            )
        if kind in self.unsupported_transition_kinds:
            raise RustRendererUnsupportedTransitionError(
                f"transition kind not implemented: {kind}"
            )
        self._transition_from = self._current_slide
        self._transition_to = to_slide_id
        self._transition_kind = kind
        self._transition_t0_ms = int(t0_ms)
        self._transition_ms = int(transition_ms)
        # Stash to-slide duration so post-promote advances are
        # plausible (the real outer loop calls begin_slide next, which
        # resets begin_t_ms anyway).
        self._duration_ms = int(to_duration_ms)


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


# ============================================================
# Bug 8 / Fix B (2026-05-17): per-slide IPC-failure throttle.
# ============================================================


@pytest.mark.asyncio
async def test_fix_b_throttle_first_fail_error_subsequent_debug(caplog):
    """A slide that raises a non-Unsupported exception at begin_slide
    must log ERROR with full traceback on the FIRST failure and DEBUG
    one-liner on SUBSEQUENT failures of the same slide_id. Prevents
    journal-spam when a permanently-broken slide sits in the playlist
    (frozen-sign incident 2026-05-17 @ 192.168.1.67 was a 1-slide
    bad-video playlist hot-spinning at ~3.4 Hz with ERROR tracebacks).
    """
    import logging

    bad = _text_slide(name="bad", text="bad", duration_ms=_FAST_DURATION_MS)

    class _RaisingFake(_FakeRustRenderer):
        """Begin_slide always raises a non-Unsupported error. Forces
        the playback loop's broad-except guard (NOT the Unsupported
        rail) so the Fix B throttle is exercised."""

        def begin_slide(
            self, slide_id: UUID, t0_ms: int, duration_ms: int
        ) -> None:
            # Record the call before raising (sanity for assertions).
            self.begin_slide_calls.append((slide_id, t0_ms, duration_ms))
            raise RustRendererSubprocessError(
                "subprocess died (simulated for Fix B throttle test)"
            )

    fake = _RaisingFake(width=8, height=8)

    loop = PlaybackLoop(
        fake,
        fetch_items=lambda: [bad],
        read_asset=lambda _id: b"",
        empty_playlist_poll_seconds=0.01,
        auto_tick_seconds=0.02,
    )

    # Capture both ERROR and DEBUG levels on the playback logger.
    with caplog.at_level(logging.DEBUG, logger="openmarquee.playback"):
        await loop.start()
        # Run long enough for the loop to attempt begin_slide ≥3 times
        # (250ms throttle between attempts → 3 attempts in ~800ms).
        await asyncio.sleep(0.85)
        await loop.stop()

    # Multiple attempts hit the bad slide.
    assert len(fake.begin_slide_calls) >= 3, (
        f"expected ≥3 begin_slide attempts on the bad slide, got "
        f"{len(fake.begin_slide_calls)}"
    )

    # Exactly ONE ERROR record per slide_id (the first failure carries
    # the traceback). Subsequent failures of the SAME id are DEBUG.
    error_records_for_bad = [
        r for r in caplog.records
        if r.levelname == "ERROR"
        and "IPC playback failed" in r.getMessage()
        and str(bad.id) in r.getMessage()
    ]
    debug_records_for_bad = [
        r for r in caplog.records
        if r.levelname == "DEBUG"
        and "throttled" in r.getMessage()
        and str(bad.id) in r.getMessage()
    ]

    assert len(error_records_for_bad) == 1, (
        f"expected exactly 1 ERROR record (first fail carries the "
        f"traceback), got {len(error_records_for_bad)}"
    )
    assert len(debug_records_for_bad) >= 1, (
        f"expected ≥1 DEBUG throttled record after the first fail, "
        f"got {len(debug_records_for_bad)}"
    )
    # The throttle set holds the failed id.
    assert bad.id in loop._failed_slide_ids


@pytest.mark.asyncio
async def test_ce225f3_broad_except_advances_past_failing_slide_to_next(caplog):
    """Bug 8 ce225f3 gap-fix: the broad-except guard at the IPC call
    site must let the outer loop ADVANCE to the next slide_id when
    one slide raises a non-Unsupported exception. The existing
    `test_fix_b_throttle_first_fail_error_subsequent_debug` uses a
    1-slide playlist, so it only proves "doesn't crash in place" —
    not "advances to next item." This test plays a 3-slide reel
    where the MIDDLE slide raises RustRendererSubprocessError and
    asserts begin_slide was called on the slide AFTER it (the loop
    didn't stall on the failing one or break out entirely).

    Regression-lock: would fail if ce225f3's `except Exception: ...
    continue` were changed to `break`, or if the broad-except were
    removed (RustRendererSubprocessError would propagate out and
    kill the task — no third-slide begin_slide call would land).
    """
    import logging

    good_a = _text_slide(name="A", text="A", duration_ms=_FAST_DURATION_MS)
    bad = _text_slide(name="bad", text="bad", duration_ms=_FAST_DURATION_MS)
    good_b = _text_slide(name="B", text="B", duration_ms=_FAST_DURATION_MS)

    class _SelectivelyRaisingFake(_FakeRustRenderer):
        """Begin_slide raises ONLY for the configured slide_id; other
        slides flow through the normal path."""

        def __init__(self, *args, raise_for: UUID, **kwargs):
            super().__init__(*args, **kwargs)
            self._raise_for = raise_for

        def begin_slide(
            self, slide_id: UUID, t0_ms: int, duration_ms: int
        ) -> None:
            self.begin_slide_calls.append((slide_id, t0_ms, duration_ms))
            if slide_id == self._raise_for:
                raise RustRendererSubprocessError(
                    "subprocess died (simulated for ce225f3 advance test)"
                )
            self._current_slide = slide_id
            self._begin_t_ms = int(t0_ms)
            self._duration_ms = int(duration_ms)

    fake = _SelectivelyRaisingFake(
        width=8, height=8, raise_for=bad.id,
    )

    loop = PlaybackLoop(
        fake,
        fetch_items=lambda: [good_a, bad, good_b],
        read_asset=lambda _id: b"",
        empty_playlist_poll_seconds=0.01,
        auto_tick_seconds=0.02,
    )

    with caplog.at_level(logging.ERROR, logger="openmarquee.playback"):
        await loop.start()
        # Long enough for begin_slide on all three: ~100ms per slide
        # for good_a + good_b at duration=100ms, plus 250ms throttle
        # on bad. Generous margin for timing variance.
        await asyncio.sleep(0.7)
        await loop.stop()

    # Begin_slide was called on all three in playlist order. The
    # critical assertion is `good_b.id` appearing in the call log —
    # that proves the loop reached the slide AFTER the failure.
    slide_ids = [c[0] for c in fake.begin_slide_calls]
    assert good_a.id in slide_ids, (
        f"good_a never begin_slide'd; loop didn't start cleanly: {slide_ids}"
    )
    assert bad.id in slide_ids, (
        f"bad.id never begin_slide'd; loop bailed before reaching it: "
        f"{slide_ids}"
    )
    assert good_b.id in slide_ids, (
        f"good_b never begin_slide'd; loop didn't advance past bad slide. "
        f"Begin_slide call log: {slide_ids}. ce225f3's broad-except "
        f"`continue` regressed to `break` or was removed."
    )

    # Order assertion: good_a → bad → good_b. (The loop iterates
    # the playlist in order; any reordering would indicate the bug
    # too.)
    ord_a = slide_ids.index(good_a.id)
    ord_bad = slide_ids.index(bad.id)
    ord_b = slide_ids.index(good_b.id)
    assert ord_a < ord_bad < ord_b, (
        f"slides processed out of order: a@{ord_a} bad@{ord_bad} b@{ord_b}"
    )


def test_ce225f3_on_loop_task_done_logs_error_on_exception(caplog):
    """Bug 8 ce225f3 gap-fix: the add_done_callback wired at start()
    must surface task exceptions via log.error with exc_info attached
    (so journalctl carries the traceback, not just a one-line label).

    Tests the `_on_loop_task_done` static method directly with a
    fake task. Regression-lock: would fail if the callback's
    log.error were dropped, downgraded to debug, or the exc_info=exc
    arg were stripped.
    """
    import logging
    from unittest.mock import MagicMock

    boom = RuntimeError("simulated _loop crash for callback test")

    fake_task = MagicMock()
    fake_task.cancelled.return_value = False
    fake_task.exception.return_value = boom

    with caplog.at_level(logging.ERROR, logger="openmarquee.playback"):
        PlaybackLoop._on_loop_task_done(fake_task)

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1, (
        f"expected exactly 1 ERROR record on exceptional task done, "
        f"got {len(error_records)}: {[r.getMessage() for r in caplog.records]}"
    )
    rec = error_records[0]
    assert "_loop task crashed" in rec.getMessage(), (
        f"error message missing 'crashed' breadcrumb: {rec.getMessage()!r}"
    )
    # exc_info attached so the traceback hits journalctl.
    assert rec.exc_info is not None, (
        "log.error was called without exc_info=exc; traceback won't "
        "reach journalctl. Reverts to ce225f3's pre-fix silent-task-death."
    )


def test_ce225f3_on_loop_task_done_logs_info_on_cancellation(caplog):
    """Bug 8 ce225f3 sibling check: a CANCELLED task (the normal
    stop() path) logs INFO, not ERROR. Without this distinction,
    every `stop()` would emit an ERROR line and operators couldn't
    distinguish a deliberate shutdown from a crash."""
    import logging
    from unittest.mock import MagicMock

    fake_task = MagicMock()
    fake_task.cancelled.return_value = True

    with caplog.at_level(logging.DEBUG, logger="openmarquee.playback"):
        PlaybackLoop._on_loop_task_done(fake_task)

    # Cancellation is INFO, not ERROR.
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    info_records = [
        r for r in caplog.records
        if r.levelname == "INFO" and "cancelled" in r.getMessage()
    ]
    assert error_records == [], (
        f"cancellation must not log ERROR; got: "
        f"{[r.getMessage() for r in error_records]}"
    )
    assert len(info_records) == 1, (
        f"expected 1 INFO 'cancelled' record, got {len(info_records)}"
    )
    # task.exception() should NOT be invoked on a cancelled task
    # (it raises CancelledError if called before .cancelled() check).
    fake_task.exception.assert_not_called()


def test_fix_b_throttle_clears_on_playlist_change(tmp_path):
    """Switching playlists clears the throttle so an operator who
    fixes a bad slide (e.g. swaps to a new playlist that includes
    the same id with a fresh asset) gets ERROR-level visibility on
    the next failure, not DEBUG-suppressed silence."""
    from uuid import uuid4

    mock = MockRenderer(8, 8, tmp_path / "out.png")
    loop = PlaybackLoop(
        mock,
        fetch_items=lambda: [],
        read_asset=lambda _id: b"",
        empty_playlist_poll_seconds=0.01,
    )
    pid_a = uuid4()
    pid_b = uuid4()
    slide_a = uuid4()
    slide_b = uuid4()

    loop._stamp_playlist_id(pid_a)
    loop._failed_slide_ids.add(slide_a)
    loop._failed_slide_ids.add(slide_b)
    assert loop._failed_slide_ids == {slide_a, slide_b}

    # Same playlist id → no clear.
    loop._stamp_playlist_id(pid_a)
    assert loop._failed_slide_ids == {slide_a, slide_b}

    # Different playlist id → clear.
    loop._stamp_playlist_id(pid_b)
    assert loop._failed_slide_ids == set()


# ============================================================
# Slice-4-followup: rust-route transitions wired via begin_transition.
# ============================================================


@pytest.mark.asyncio
async def test_non_cut_transition_calls_begin_transition_for_each_slide():
    """When the playlist has slides with non-cut transitions, the
    loop calls begin_transition INTO the next slide for each one.
    Pins the slice-4-followup wire-up: cut path stayed for slice 4;
    this asserts the new path fires when the transition spec asks
    for it."""
    text_a = _text_slide(name="A", text="A", duration_ms=_FAST_DURATION_MS,
                         transition="fade", transition_ms=30)
    text_b = _text_slide(name="B", text="B", duration_ms=_FAST_DURATION_MS,
                         transition="wipe", transition_ms=30)

    fake = _FakeRustRenderer(width=8, height=8)
    loop = PlaybackLoop(
        fake,
        fetch_items=lambda: [text_a, text_b],
        read_asset=lambda _id: b"",
        empty_playlist_poll_seconds=0.01,
        auto_tick_seconds=0.02,
    )
    await loop.start()
    await asyncio.sleep(0.4)
    await loop.stop()

    # At least one fade + one wipe transition fired -- the loop went
    # through the playlist at least once. Each begin_transition entry:
    # (to_slide_id, to_duration_ms, kind, transition_ms, t0_ms).
    kinds_seen = [c[2] for c in fake.begin_transition_calls]
    assert "fade" in kinds_seen, (
        f"expected fade transition; got {kinds_seen}"
    )
    assert "wipe" in kinds_seen, (
        f"expected wipe transition; got {kinds_seen}"
    )
    # fade transition's to_slide is text_b; wipe's to_slide is text_a
    # (playlist wrap). Pin both.
    for to_id, _dur, kind, _ms, _t0 in fake.begin_transition_calls:
        if kind == "fade":
            assert to_id == text_b.id
        elif kind == "wipe":
            assert to_id == text_a.id


@pytest.mark.asyncio
async def test_cut_transition_does_not_call_begin_transition():
    """transition='cut' (or unset) means instant cut between slides.
    begin_transition MUST NOT fire -- the outer loop's begin_slide on
    the next iteration IS the instant cut."""
    text_a = _text_slide(name="A", text="A", duration_ms=_FAST_DURATION_MS,
                         transition="cut", transition_ms=0)
    text_b = _text_slide(name="B", text="B", duration_ms=_FAST_DURATION_MS,
                         transition="cut", transition_ms=0)

    fake = _FakeRustRenderer(width=8, height=8)
    loop = PlaybackLoop(
        fake,
        fetch_items=lambda: [text_a, text_b],
        read_asset=lambda _id: b"",
        empty_playlist_poll_seconds=0.01,
        auto_tick_seconds=0.02,
    )
    await loop.start()
    await asyncio.sleep(0.35)
    await loop.stop()

    assert fake.begin_transition_calls == [], (
        f"cut transitions should not call begin_transition; got "
        f"{fake.begin_transition_calls}"
    )
    # begin_slide still fires for each slide.
    assert len(fake.begin_slide_calls) >= 2


@pytest.mark.asyncio
async def test_zero_duration_transition_does_not_call_begin_transition():
    """A non-cut kind name with transition_ms=0 is also a cut (no
    blend window to drive). Edge case from the dispatch's test plan."""
    text_a = _text_slide(name="A", text="A", duration_ms=_FAST_DURATION_MS,
                         transition="fade", transition_ms=0)
    text_b = _text_slide(name="B", text="B", duration_ms=_FAST_DURATION_MS,
                         transition="fade", transition_ms=0)

    fake = _FakeRustRenderer(width=8, height=8)
    loop = PlaybackLoop(
        fake,
        fetch_items=lambda: [text_a, text_b],
        read_asset=lambda _id: b"",
        empty_playlist_poll_seconds=0.01,
        auto_tick_seconds=0.02,
    )
    await loop.start()
    await asyncio.sleep(0.3)
    await loop.stop()

    assert fake.begin_transition_calls == [], (
        f"0-duration transitions should not call begin_transition; "
        f"got {fake.begin_transition_calls}"
    )


@pytest.mark.asyncio
async def test_unsupported_transition_falls_through_to_cut(caplog):
    """If the sidecar raises RustRendererUnsupportedTransitionError
    on begin_transition (forward-compat: Rust silently FS_CUTs today,
    but a future explicit-error path is provisioned), the loop logs
    + falls through to instant cut. The next slide still begin_slides
    on the next iteration -- visible result is a cut."""
    import logging

    # Pydantic gates the schema's `transition` to the existing 16 kind
    # names, so we can't construct a slide with a truly-unknown kind.
    # Instead: use a real kind ("glitch") and have the fake renderer
    # raise UnsupportedTransitionError for it, simulating the future
    # case where a Rust regression unwires a kind that the schema
    # still accepts.
    text_a = _text_slide(name="A", text="A", duration_ms=_FAST_DURATION_MS,
                         transition="glitch", transition_ms=30)
    text_b = _text_slide(name="B", text="B", duration_ms=_FAST_DURATION_MS,
                         transition="cut", transition_ms=0)

    fake = _FakeRustRenderer(
        width=8, height=8,
        unsupported_transition_kinds={"glitch"},
    )
    loop = PlaybackLoop(
        fake,
        fetch_items=lambda: [text_a, text_b],
        read_asset=lambda _id: b"",
        empty_playlist_poll_seconds=0.01,
        auto_tick_seconds=0.02,
    )
    with caplog.at_level(logging.INFO, logger="openmarquee.playback"):
        await loop.start()
        await asyncio.sleep(0.3)
        await loop.stop()

    # begin_transition WAS called (we recorded the call before the raise).
    fake_calls = [c for c in fake.begin_transition_calls
                  if c[2] == "glitch"]
    assert fake_calls, "begin_transition should have been attempted"
    # Loop logged the fallback.
    skip_logs = [
        r for r in caplog.records
        if "falling through to instant cut" in r.getMessage()
    ]
    assert skip_logs, (
        f"expected fall-through log; got {[r.getMessage() for r in caplog.records]}"
    )
    # Loop didn't crash -- both slides reached begin_slide.
    slide_ids = [c[0] for c in fake.begin_slide_calls]
    assert text_a.id in slide_ids
    assert text_b.id in slide_ids


@pytest.mark.asyncio
async def test_subprocess_error_during_begin_transition_swaps_to_mock():
    """Slice 4 invariant regression: if the proxy raises a generic
    SubprocessError during begin_transition (the subprocess died),
    AutoFallbackRenderer DOES swap to Mock. Distinguishes the
    "transition kind unsupported" path (log + fall through) from
    the "proxy busted" path (one-way swap)."""
    from openmarquee.dependencies import (
        AutoFallbackInMockError,
        AutoFallbackRenderer,
        _mock_renderer_singleton,
    )

    fake = _FakeRustRenderer(
        width=8, height=8,
        subprocess_error_on_begin_transition=True,
    )
    wrapper = AutoFallbackRenderer(fake, _mock_renderer_singleton)
    # Calling begin_transition on the wrapper should raise the
    # AutoFallbackInMockError (per slice 4's contract -- subprocess-
    # error path wraps the raise) AND swap to Mock.
    with pytest.raises(AutoFallbackInMockError):
        wrapper.begin_transition(
            UUID("00000000-0000-0000-0000-000000000001"),
            5000, "fade", 30, 0,
        )
    assert wrapper.is_in_fallback is True
