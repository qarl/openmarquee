"""Phase 12.1 stream takeover coverage (SYSTEM_SPEC §5.11).

Four scenarios called out in IMPLEMENTATION_PLAN Phase 12.1:

1. Frame format — incoming WebRTC frames are downscaled to renderer
   dims and pushed as RGB888 (matching §7.6 wire format).
2. Single-publisher — second concurrent /start raises StreamAlreadyActive.
3. Takeover — /takeover force-stops the existing session and starts new.
4. Pause-and-resume — PlaybackLoop pauses on stream activate and resumes
   the same slide it was on when the session ends.

aiortc's RTCPeerConnection is patched out for the orchestration tests
because the PC's actual SDP/ICE machinery is irrelevant here — the
StreamManager+StreamSession state and the PlaybackLoop pause/resume
integration are what we're verifying. Real aiortc gets a live-fire
exercise in Phase 12.3 (hardware bring-up) instead.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any
from unittest.mock import patch
from uuid import UUID

import av
import numpy as np
import pytest
from PIL import Image

from openmarquee.content import TextSlide
from openmarquee.playback import PlaybackLoop
from openmarquee.rendering.mock import MockRenderer
from openmarquee.stream import (
    RtspStartRequest,
    StreamAlreadyActive,
    StreamManager,
    StreamNotActive,
    StreamSession,
    WebRtcStartRequest,
)
from openmarquee.stream_source import WebRtcStreamSource

_FAST_DURATION_MS = 100
_FAST_EMPTY_POLL = 0.01


# --- Fakes -----------------------------------------------------------------


class _FakeSdp:
    """Stand-in for aiortc's RTCSessionDescription. Only `sdp` and `type`
    are consumed by StreamSession.start, so a thin shim suffices."""

    def __init__(self, sdp: str, type: str):
        self.sdp = sdp
        self.type = type


class _FakeRTCPeerConnection:
    """Records calls + serves a canned SDP answer.

    Doesn't try to imitate ICE, codec negotiation, or media flow — the
    frame-consumer side is exercised directly by feeding av.VideoFrames
    to a WebRtcStreamSource in test_webrtc_source_yields_rgb888.
    """

    answer_sdp = "v=0\r\nfake-answer\r\n"

    def __init__(self):
        self.localDescription: _FakeSdp | None = None
        self.remoteDescription: _FakeSdp | None = None
        self.handlers: dict[str, Any] = {}
        self.closed = False

    def on(self, event: str):
        def decorator(fn):
            self.handlers[event] = fn
            return fn

        return decorator

    async def setRemoteDescription(self, desc):
        self.remoteDescription = desc

    async def createAnswer(self):
        return _FakeSdp(self.answer_sdp, "answer")

    async def setLocalDescription(self, desc):
        self.localDescription = desc

    async def close(self):
        self.closed = True


class _FakeTrack:
    """One-shot iterator over canned av.VideoFrames.

    WebRtcStreamSource.frames() runs `while not self._closed: await
    track.recv()`, so once frames are exhausted the recv() raises and
    the source exits its outer try/except. Mirrors how aiortc handles
    a track ending (MediaStreamError).
    """

    kind = "video"

    def __init__(self, frames):
        self._frames = list(frames)
        self._index = 0

    async def recv(self):
        if self._index >= len(self._frames):
            # Yield to other tasks before "ending" so the consumer's
            # render of the last frame has a chance to run before we
            # tear down.
            await asyncio.sleep(0)
            raise StopAsyncIteration("track ended")
        frame = self._frames[self._index]
        self._index += 1
        return frame


# --- Helpers ---------------------------------------------------------------


def _video_frame(width: int, height: int, fill: int = 128) -> av.VideoFrame:
    """Build a single VideoFrame of given dims at a uniform gray fill.

    rgb24 format means PyAV stores the bytes the same way render_frame
    expects — uniform gray = (fill, fill, fill) for every pixel.
    """
    arr = np.full((height, width, 3), fill, dtype=np.uint8)
    return av.VideoFrame.from_ndarray(arr, format="rgb24")


def _png_bytes(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_loop_with_three_slides(
    tmp_path,
) -> tuple[PlaybackLoop, MockRenderer, list[TextSlide]]:
    """Standard playlist: A, B, C — each TextSlide rendered at 8×8 px."""
    renderer = MockRenderer(8, 8, tmp_path / "out.png")
    slides = [
        TextSlide(name=name, text=name, duration_ms=_FAST_DURATION_MS) for name in ("A", "B", "C")
    ]
    pngs = {
        slides[0].id: _png_bytes(8, 8, (255, 0, 0)),
        slides[1].id: _png_bytes(8, 8, (0, 255, 0)),
        slides[2].id: _png_bytes(8, 8, (0, 0, 255)),
    }

    def fetch():
        return list(slides)

    def read_asset(item_id: UUID) -> bytes:
        return pngs[item_id]

    loop = PlaybackLoop(
        renderer=renderer,
        fetch_items=fetch,
        read_asset=read_asset,
        empty_playlist_poll_seconds=_FAST_EMPTY_POLL,
    )
    return loop, renderer, slides


async def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.01):
    """Poll `predicate` until truthy or `timeout` elapses."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


# --- 1. Frame format -------------------------------------------------------


@pytest.mark.asyncio
async def test_webrtc_source_yields_rgb888_at_renderer_dims(tmp_path):
    """WebRtcStreamSource downscales frames of any size to the
    renderer's native dims and yields them as RGB888 — same wire
    format §7.6 mandates. Without this guarantee, mixing stream +
    playlist sources would produce frames the renderer rejects."""
    renderer = MockRenderer(8, 8, tmp_path / "out.png")
    source = WebRtcStreamSource(renderer)
    # Source frame is 320×240 (different from the 8×8 renderer) so the
    # cover-fit path is exercised. The FakeTrack's recv() raises once
    # exhausted; the source's broad `except Exception` treats that as
    # a clean track-end (mirrors aiortc's MediaStreamError).
    source.set_track(_FakeTrack([_video_frame(320, 240, fill=128)]))

    captured = [frame async for frame in source.frames()]

    assert captured, "source didn't yield any frame"
    # RGB888 contract: width * height * 3 bytes. Renderer is 8×8 → 192.
    assert len(captured[0]) == 8 * 8 * 3
    # Solid gray input → solid gray output (no swizzle, no channel
    # reorder); confirms RGB byte order is preserved end-to-end.
    assert captured[0] == bytes([128] * (8 * 8 * 3))


@pytest.mark.asyncio
async def test_session_pump_pushes_source_frames_to_renderer(tmp_path):
    """End-to-end: a StreamSession's pump drives its WebRtcStreamSource
    and pushes each yielded frame to renderer.render_frame(). Drives
    the on_track callback directly (the fake PC never negotiates real
    media) so the pump path itself is exercised."""
    renderer = MockRenderer(8, 8, tmp_path / "out.png")
    loop = PlaybackLoop(
        renderer=renderer,
        fetch_items=lambda: [],
        read_asset=lambda _id: b"",
        empty_playlist_poll_seconds=_FAST_EMPTY_POLL,
    )
    await loop.start()
    try:
        with patch("openmarquee.stream.RTCPeerConnection", _FakeRTCPeerConnection):
            session = StreamSession(loop)
            captured: list[bytes] = []
            original_render = renderer.render_frame
            renderer.render_frame = lambda data: captured.append(data) or original_render(data)

            await session.start_webrtc("v=0\r\noffer\r\n")
            # Fire the captured on_track handler — the fake PC records
            # it but never invokes it (no real ICE/DTLS/SRTP).
            on_track = session._pc.handlers["track"]
            on_track(_FakeTrack([_video_frame(320, 240, fill=128)]))
            # Pump runs the source to exhaustion, then completes.
            assert session._pump_task is not None
            await session._pump_task
            await session.close()
    finally:
        await loop.stop()

    assert captured, "pump didn't push any frame to the renderer"
    assert len(captured[0]) == 8 * 8 * 3
    assert captured[0] == bytes([128] * (8 * 8 * 3))


# --- 2. Single-publisher ---------------------------------------------------


@pytest.mark.asyncio
async def test_second_start_while_active_raises_stream_already_active(tmp_path):
    """Second concurrent /start is rejected with the active session id
    so the phone can switch to the take-over affordance without polling
    /status separately."""
    loop, _renderer, _ = _make_loop_with_three_slides(tmp_path)
    await loop.start()
    try:
        with patch("openmarquee.stream.RTCPeerConnection", _FakeRTCPeerConnection):
            manager = StreamManager(loop)
            session_id, _answer = await manager.start(WebRtcStartRequest(sdp_offer="v=0\r\noffer-1\r\n"))
            assert manager.is_active

            with pytest.raises(StreamAlreadyActive) as exc_info:
                await manager.start(WebRtcStartRequest(sdp_offer="v=0\r\noffer-2\r\n"))
            assert exc_info.value.active_session_id == session_id

            # First session is still the active one — refused start
            # didn't tear it down.
            assert manager.active_session_id == session_id
    finally:
        await manager.stop_all()
        await loop.stop()


# --- 3. Takeover -----------------------------------------------------------


@pytest.mark.asyncio
async def test_takeover_replaces_active_session_with_new_id(tmp_path):
    """/takeover force-closes the existing session and starts a fresh
    one. New session has a different id; old session is closed."""
    loop, _renderer, _ = _make_loop_with_three_slides(tmp_path)
    await loop.start()
    try:
        with patch("openmarquee.stream.RTCPeerConnection", _FakeRTCPeerConnection):
            manager = StreamManager(loop)
            first_id, _ = await manager.start(WebRtcStartRequest(sdp_offer="v=0\r\noffer-1\r\n"))
            first_session = manager._session
            assert first_session is not None

            second_id, _ = await manager.takeover(WebRtcStartRequest(sdp_offer="v=0\r\noffer-2\r\n"))

            assert second_id != first_id
            assert first_session.closed
            assert manager.active_session_id == second_id
    finally:
        await manager.stop_all()
        await loop.stop()


@pytest.mark.asyncio
async def test_takeover_with_no_active_session_just_starts(tmp_path):
    """Takeover doesn't require something to be active — phone hits it
    when the user ack'd the warning, but the previous session may have
    naturally ended in the interim."""
    loop, _renderer, _ = _make_loop_with_three_slides(tmp_path)
    await loop.start()
    try:
        with patch("openmarquee.stream.RTCPeerConnection", _FakeRTCPeerConnection):
            manager = StreamManager(loop)
            assert not manager.is_active
            session_id, _ = await manager.takeover(WebRtcStartRequest(sdp_offer="v=0\r\noffer\r\n"))
            assert manager.is_active
            assert manager.active_session_id == session_id
    finally:
        await manager.stop_all()
        await loop.stop()


@pytest.mark.asyncio
async def test_stop_unknown_session_raises(tmp_path):
    """Stop with a session id that isn't the active one (or no active
    session at all) is a 404 case — phone caller has stale state."""
    loop, _renderer, _ = _make_loop_with_three_slides(tmp_path)
    await loop.start()
    try:
        with patch("openmarquee.stream.RTCPeerConnection", _FakeRTCPeerConnection):
            manager = StreamManager(loop)
            from uuid import uuid4

            with pytest.raises(StreamNotActive):
                await manager.stop(uuid4())
    finally:
        await loop.stop()


# --- 4. Pause-and-resume ---------------------------------------------------


@pytest.mark.asyncio
async def test_pause_resume_returns_to_same_slide(tmp_path):
    """Stream takeover pauses the loop mid-cycle; resume puts the same
    slide back on screen. Per §5.11: pause+resume, not restart-from-
    slide-start (sub-second position-within-slide isn't tracked, but
    the slide identity is)."""
    loop, _renderer, slides = _make_loop_with_three_slides(tmp_path)
    await loop.start()
    try:
        # Wait until the loop is on the SECOND slide (not the first —
        # we want a non-trivial mid-cycle index).
        ok = await _wait_until(lambda: loop.current_item_id == slides[1].id, timeout=3.0)
        assert ok, "loop never reached slide B"

        await loop.pause()
        # Pause is request-shaped: the loop notices on the next iteration
        # of _wait or the for-loop's pause-check. Poll until is_paused
        # AND the loop has actually saved a resume index (proving the
        # for-loop noticed and broke).
        await _wait_until(lambda: loop.is_paused and loop._resume_at_index is not None)
        assert loop.is_paused
        # _resume_at_index points at the for-loop iteration that was
        # interrupted — slide B is index 1.
        assert loop._resume_at_index == 1

        await loop.resume()
        assert not loop.is_paused

        # After resume, the loop fetches items again and starts the
        # for-loop at the saved index. Verify it lands back on slide B
        # before progressing further.
        ok = await _wait_until(lambda: loop.current_item_id == slides[1].id, timeout=3.0)
        assert ok, "loop didn't resume on slide B"
    finally:
        await loop.stop()


@pytest.mark.asyncio
async def test_pause_when_loop_not_running_is_noop(tmp_path):
    """Pause/resume on a stopped loop quietly does nothing — the public
    API stays safe to call from anywhere without lifecycle assertions."""
    renderer = MockRenderer(8, 8, tmp_path / "out.png")
    loop = PlaybackLoop(
        renderer=renderer,
        fetch_items=lambda: [],
        read_asset=lambda _id: b"",
        empty_playlist_poll_seconds=_FAST_EMPTY_POLL,
    )
    # No start() called — events are None.
    await loop.pause()
    assert not loop.is_paused
    await loop.resume()
    assert not loop.is_paused


@pytest.mark.asyncio
async def test_phantom_session_watchdog_closes_on_no_track(tmp_path, monkeypatch):
    """Phase 12.1 Finding #2: a bogus SDP that parses + answers cleanly
    but never delivers a real track produces a "phantom" session — the
    PC is open, StreamManager.is_active=True, but no media flows. The
    phantom-session watchdog should auto-close after _PHANTOM_TIMEOUT_
    SECONDS, flipping closed=True so is_active turns False on the next
    /status query.

    The fake PC's on() decorator captures the on_track handler but
    never invokes it (no real ICE / DTLS / SRTP), simulating exactly
    the "answered but no track" failure mode."""
    # Compress the watchdog timeout so the test runs in <0.5s.
    monkeypatch.setattr(StreamSession, "_PHANTOM_TIMEOUT_SECONDS", 0.1)

    loop, _renderer, _ = _make_loop_with_three_slides(tmp_path)
    await loop.start()
    try:
        with patch("openmarquee.stream.RTCPeerConnection", _FakeRTCPeerConnection):
            session = StreamSession(loop)
            await session.start_webrtc("v=0\r\nbogus-no-media\r\n")
            assert not session.closed
            # Wait past the watchdog timeout. on_track never fires
            # (the fake PC doesn't actually negotiate media), so the
            # watchdog should hit its TimeoutError path and call
            # close().
            await _wait_until(lambda: session.closed, timeout=1.0)
            assert session.closed
            # Playback resumed as part of close(); no phantom paused-
            # forever side effect.
            assert not loop.is_paused
    finally:
        await loop.stop()


@pytest.mark.asyncio
async def test_phantom_watchdog_canceled_on_normal_close(tmp_path, monkeypatch):
    """Normal close() path (operator stops the stream before the
    watchdog timer fires) cancels the watchdog so it doesn't dangle
    + race with the explicit close. Without this, the watchdog could
    fire its own log.warning ('phantom') even on a clean close."""
    monkeypatch.setattr(StreamSession, "_PHANTOM_TIMEOUT_SECONDS", 5.0)

    loop, _renderer, _ = _make_loop_with_three_slides(tmp_path)
    await loop.start()
    try:
        with patch("openmarquee.stream.RTCPeerConnection", _FakeRTCPeerConnection):
            session = StreamSession(loop)
            await session.start_webrtc("v=0\r\noffer\r\n")
            # Close before the 5s timeout would fire.
            await session.close()
            assert session.closed
            # Watchdog should be done (cancelled) — not running, not
            # pending. Ensures close() awaited the cancellation.
            assert session._watchdog_task is not None
            assert session._watchdog_task.done()
    finally:
        await loop.stop()


@pytest.mark.asyncio
async def test_streamsession_start_pauses_playback_close_resumes(tmp_path):
    """Direct integration: StreamSession.start_webrtc() pauses the
    loop, StreamSession.close() resumes. This is the contract
    StreamManager relies on — verified separately from the manager so
    a manager-side bug doesn't mask a session-side regression."""
    loop, _renderer, _ = _make_loop_with_three_slides(tmp_path)
    await loop.start()
    try:
        with patch("openmarquee.stream.RTCPeerConnection", _FakeRTCPeerConnection):
            session = StreamSession(loop)
            await session.start_webrtc("v=0\r\noffer\r\n")
            await _wait_until(lambda: loop.is_paused)
            assert loop.is_paused

            await session.close()
            assert not loop.is_paused
    finally:
        await loop.stop()


# --- 5. RTSP takeover (STREAM/VLC slice 4) ---------------------------------


def _empty_loop(tmp_path) -> tuple[PlaybackLoop, MockRenderer]:
    """A running-able loop with an empty playlist — it renders nothing
    on its own, so a test's render_frame capture sees only stream
    frames."""
    renderer = MockRenderer(8, 8, tmp_path / "out.png")
    loop = PlaybackLoop(
        renderer=renderer,
        fetch_items=lambda: [],
        read_asset=lambda _id: b"",
        empty_playlist_poll_seconds=_FAST_EMPTY_POLL,
    )
    return loop, renderer


def _patch_mock_ffmpeg(monkeypatch, tmp_path, *, n_frames: int, frame_size: int):
    """Point RtspStreamSource's VlcRtspConsumer at a mock-ffmpeg
    binary that emits `n_frames` frames of `frame_size` bytes."""
    import functools

    from openmarquee.vlc_rtsp_consumer import VlcRtspConsumer
    from tests.test_vlc_rtsp_consumer import _write_mock_ffmpeg

    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=frame_size, n_frames=n_frames
    )
    monkeypatch.setattr(
        "openmarquee.stream_source.VlcRtspConsumer",
        functools.partial(VlcRtspConsumer, ffmpeg_bin=mock),
    )


@pytest.mark.asyncio
async def test_start_rtsp_session_pulls_frames(tmp_path, monkeypatch):
    """End-to-end: StreamManager.start() with an RtspStartRequest
    spawns an RtspStreamSource, pumps the (mock) ffmpeg's frames to
    the renderer, and pauses the playlist. The slice-4 gate."""
    frame_size = 8 * 8 * 3
    _patch_mock_ffmpeg(monkeypatch, tmp_path, n_frames=4, frame_size=frame_size)
    loop, renderer = _empty_loop(tmp_path)
    await loop.start()
    captured: list[bytes] = []
    original_render = renderer.render_frame
    renderer.render_frame = lambda data: captured.append(data) or original_render(data)
    try:
        manager = StreamManager(loop)
        session_id, answer = await manager.start(
            RtspStartRequest(url="rtsp://laptop:8554/live")
        )
        # RTSP has no SDP answer to hand back.
        assert answer is None
        assert manager.is_active
        assert manager.active_session_id == session_id
        # The takeover paused the playlist.
        assert await _wait_until(lambda: loop.is_paused)
        # The pump drains the mock ffmpeg's 4 frames to the renderer.
        assert await _wait_until(lambda: len(captured) >= 4)
        assert len(captured) == 4
        assert all(len(f) == frame_size for f in captured)
    finally:
        await manager.stop_all()
        await loop.stop()
    # close() resumed playback.
    assert not loop.is_paused


@pytest.mark.asyncio
async def test_rtsp_session_has_no_peer_connection(tmp_path, monkeypatch):
    """An RTSP takeover uses no RTCPeerConnection and arms no
    phantom-track watchdog — that machinery is WebRTC-only."""
    _patch_mock_ffmpeg(monkeypatch, tmp_path, n_frames=1, frame_size=8 * 8 * 3)
    loop, _renderer = _empty_loop(tmp_path)
    await loop.start()
    try:
        session = StreamSession(loop)
        await session.start_rtsp("rtsp://laptop:8554/live")
        assert session._pc is None
        assert session._watchdog_task is None
        await session.close()
        assert not loop.is_paused
    finally:
        await loop.stop()
