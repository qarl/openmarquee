"""Phase 12.1 live takeover coverage (SYSTEM_SPEC §5.11).

Four scenarios called out in IMPLEMENTATION_PLAN Phase 12.1:

1. Frame format — incoming WebRTC frames are downscaled to renderer
   dims and pushed as RGB888 (matching §7.6 wire format).
2. Single-publisher — second concurrent /start raises LiveAlreadyActive.
3. Takeover — /takeover force-stops the existing session and starts new.
4. Pause-and-resume — PlaybackLoop pauses on live-takeover activate and
   resumes the same slide it was on when the session ends.

aiortc's RTCPeerConnection is patched out for the orchestration tests
because the PC's actual SDP/ICE machinery is irrelevant here — the
LiveManager+LiveSession state and the PlaybackLoop pause/resume
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
from openmarquee.live import (
    LiveAlreadyActive,
    LiveManager,
    LiveNotActive,
    LiveSession,
    StreamSourceUnreachable,
    StreamStartRequest,
    WebRtcStartRequest,
)
from openmarquee.playback import PlaybackLoop
from openmarquee.rendering.mock import MockRenderer
from openmarquee.stream_source import WebRtcStreamSource

_FAST_DURATION_MS = 100
_FAST_EMPTY_POLL = 0.01


# --- Fakes -----------------------------------------------------------------


class _FakeSdp:
    """Stand-in for aiortc's RTCSessionDescription. Only `sdp` and `type`
    are consumed by LiveSession.start, so a thin shim suffices."""

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
        # 2026-05-24 D3 Option C: tests fire the connectionstatechange
        # handler after mutating this field so the LiveSession callback
        # reads the simulated state. Default to "new" matching the
        # WebRTC spec's pre-negotiation state.
        self.connectionState: str = "new"

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


def _write_mock_ffprobe(tmp_path, *, width: int, height: int):
    """Write an executable mock-ffprobe that reports `(width, height)`
    as the JSON ffprobe -of json output, and return its path."""
    import json
    import stat
    import sys

    probe = tmp_path / "ffprobe"
    body = f"#!{sys.executable}\n"
    body += "import sys\n"
    body += (
        "sys.stdout.write("
        + repr(json.dumps({"streams": [{"width": width, "height": height}]}))
        + ")\n"
    )
    probe.write_text(body)
    probe.chmod(probe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(probe)


@pytest.mark.asyncio
async def test_ffmpeg_source_frame_dims_clamps_over_large_source(tmp_path):
    """Renderer-hardening C2 (finding H2): `FfmpegStreamSource.
    frame_dims()` reports the texture-limit-CLAMPED dims when ffprobe
    discovers a source larger than the vc4 GPU's 2048-px cap — so the
    renderer's H2(a) over-large guard is never tripped by a properly-
    clamped stream — and the consumer's ffmpeg argv gains a `scale`
    filter to downscale the decoded frames."""
    import functools

    from openmarquee.stream_consumer import StreamConsumer
    from openmarquee.stream_source import FfmpegStreamSource

    # ffprobe reports a 4K source (3840x2160), well over the 2048 cap.
    probe = _write_mock_ffprobe(tmp_path, width=3840, height=2160)
    # ffmpeg points at a non-existent binary — frames() exits cleanly
    # after the probe; we assert frame_dims + argv, not frame yield.
    consumer_cls = functools.partial(
        StreamConsumer,
        ffmpeg_bin=str(tmp_path / "unused-ffmpeg"),
        ffprobe_bin=probe,
    )
    with patch("openmarquee.stream_source.StreamConsumer", consumer_cls):
        renderer = MockRenderer(1920, 1080, tmp_path / "out.png")
        source = FfmpegStreamSource(renderer, "rtsp://laptop:8554/live")
        # Before any frame, ffprobe has not run — dims unknown.
        assert source.frame_dims() is None
        # Drain frames() — runs ffprobe, then exits (ffmpeg missing).
        [f async for f in source.frames()]
        await source.close()

    dims = source.frame_dims()
    assert dims is not None
    w, h = dims
    # 3840x2160 scaled by 2048/3840 -> 2048x1152: within the cap,
    # aspect preserved, both even (NV12 4:2:0).
    assert (w, h) == (2048, 1152)
    assert w <= 2048 and h <= 2048
    assert w % 2 == 0 and h % 2 == 0
    # The consumer's ffmpeg argv carries the downscale `scale` filter.
    argv = source._consumer._build_argv()
    assert argv[argv.index("-vf") + 1] == "scale=2048:1152"


@pytest.mark.asyncio
async def test_ffmpeg_source_frame_dims_unchanged_for_in_limit_source(tmp_path):
    """A normal <=2048 source passes straight through: `frame_dims()`
    reports the raw ffprobe dims and the consumer's ffmpeg argv has NO
    `scale` filter — the unchanged HW-decode path, no swscale cost."""
    import functools

    from openmarquee.stream_consumer import StreamConsumer
    from openmarquee.stream_source import FfmpegStreamSource

    probe = _write_mock_ffprobe(tmp_path, width=1920, height=1080)
    consumer_cls = functools.partial(
        StreamConsumer,
        ffmpeg_bin=str(tmp_path / "unused-ffmpeg"),
        ffprobe_bin=probe,
    )
    with patch("openmarquee.stream_source.StreamConsumer", consumer_cls):
        renderer = MockRenderer(1280, 720, tmp_path / "out.png")
        source = FfmpegStreamSource(renderer, "rtsp://laptop:8554/live")
        [f async for f in source.frames()]
        await source.close()

    # In-limit source dims pass through unchanged.
    assert source.frame_dims() == (1920, 1080)
    argv = source._consumer._build_argv()
    assert "-vf" not in argv
    assert "scale" not in " ".join(argv)


@pytest.mark.asyncio
async def test_session_pump_pushes_source_frames_to_renderer(tmp_path):
    """End-to-end: a LiveSession's pump drives its WebRtcStreamSource
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
        with patch("openmarquee.live.RTCPeerConnection", _FakeRTCPeerConnection):
            session = LiveSession(loop)
            captured: list[bytes] = []
            original_render = renderer.render_frame

            def _record(data, **kwargs):
                captured.append(data)
                return original_render(data, **kwargs)

            renderer.render_frame = _record

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
    # STREAM/VLC slice 2.5: the pump ends the renderer's frame-pump
    # session on exit (here, track-end) — exactly once.
    assert renderer.end_external_frames_calls == 1


@pytest.mark.asyncio
async def test_session_pump_stops_and_logs_once_on_render_failure(tmp_path, caplog):
    """If render_frame raises (a Rust sidecar without the push-frames
    IPC op), the pump logs ONCE and stops — it does NOT log a
    traceback per frame at the stream's frame rate. Guards against the
    per-frame-logging flood that regressed FPS on the production
    sign."""
    renderer = MockRenderer(8, 8, tmp_path / "out.png")
    loop = PlaybackLoop(
        renderer=renderer,
        fetch_items=lambda: [],
        read_asset=lambda _id: b"",
        empty_playlist_poll_seconds=_FAST_EMPTY_POLL,
    )
    await loop.start()
    try:
        with patch("openmarquee.live.RTCPeerConnection", _FakeRTCPeerConnection):
            session = LiveSession(loop)
            render_calls = {"n": 0}

            def boom(_data, **_kwargs):
                render_calls["n"] += 1
                raise NotImplementedError("renderer can't push-render")

            renderer.render_frame = boom

            await session.start_webrtc("v=0\r\noffer\r\n")
            on_track = session._pc.handlers["track"]
            # A 5-frame track: without the once-and-stop guard the
            # pump would call render_frame (and log) all 5 times.
            on_track(_FakeTrack([_video_frame(8, 8) for _ in range(5)]))
            assert session._pump_task is not None
            await session._pump_task
            await session.close()
    finally:
        await loop.stop()

    # render_frame raised on the first frame; the pump stopped — it
    # did NOT keep calling render_frame for the remaining 4 frames.
    assert render_calls["n"] == 1
    # Exactly one error line about the rejected frame — not five.
    rejected_logs = [
        r
        for r in caplog.records
        if r.levelname == "ERROR" and "rejected a pushed frame" in r.message
    ]
    assert len(rejected_logs) == 1


# --- 2. Single-publisher ---------------------------------------------------


@pytest.mark.asyncio
async def test_second_start_while_active_raises_live_already_active(tmp_path):
    """Second concurrent /start is rejected with the active session id
    so the phone can switch to the take-over affordance without polling
    /status separately."""
    loop, _renderer, _ = _make_loop_with_three_slides(tmp_path)
    await loop.start()
    try:
        with patch("openmarquee.live.RTCPeerConnection", _FakeRTCPeerConnection):
            manager = LiveManager(loop)
            session_id, _answer = await manager.start(
                WebRtcStartRequest(sdp_offer="v=0\r\noffer-1\r\n")
            )
            assert manager.is_active

            with pytest.raises(LiveAlreadyActive) as exc_info:
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
        with patch("openmarquee.live.RTCPeerConnection", _FakeRTCPeerConnection):
            manager = LiveManager(loop)
            first_id, _ = await manager.start(WebRtcStartRequest(sdp_offer="v=0\r\noffer-1\r\n"))
            first_session = manager._session
            assert first_session is not None

            second_id, _ = await manager.takeover(
                WebRtcStartRequest(sdp_offer="v=0\r\noffer-2\r\n")
            )

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
        with patch("openmarquee.live.RTCPeerConnection", _FakeRTCPeerConnection):
            manager = LiveManager(loop)
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
        with patch("openmarquee.live.RTCPeerConnection", _FakeRTCPeerConnection):
            manager = LiveManager(loop)
            from uuid import uuid4

            with pytest.raises(LiveNotActive):
                await manager.stop(uuid4())
    finally:
        await loop.stop()


# --- 4. Pause-and-resume ---------------------------------------------------


@pytest.mark.asyncio
async def test_pause_resume_returns_to_same_slide(tmp_path):
    """Live takeover pauses the loop mid-cycle; resume puts the same
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
    PC is open, LiveManager.is_active=True, but no media flows. The
    phantom-session watchdog should auto-close after _PHANTOM_TIMEOUT_
    SECONDS, flipping closed=True so is_active turns False on the next
    /status query.

    The fake PC's on() decorator captures the on_track handler but
    never invokes it (no real ICE / DTLS / SRTP), simulating exactly
    the "answered but no track" failure mode."""
    # Compress the watchdog timeout so the test runs in <0.5s.
    monkeypatch.setattr(LiveSession, "_PHANTOM_TIMEOUT_SECONDS_WEBRTC", 0.1)

    loop, _renderer, _ = _make_loop_with_three_slides(tmp_path)
    await loop.start()
    try:
        with patch("openmarquee.live.RTCPeerConnection", _FakeRTCPeerConnection):
            session = LiveSession(loop)
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
    monkeypatch.setattr(LiveSession, "_PHANTOM_TIMEOUT_SECONDS_WEBRTC", 5.0)

    loop, _renderer, _ = _make_loop_with_three_slides(tmp_path)
    await loop.start()
    try:
        with patch("openmarquee.live.RTCPeerConnection", _FakeRTCPeerConnection):
            session = LiveSession(loop)
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
    """Direct integration: LiveSession.start_webrtc() pauses the
    loop, LiveSession.close() resumes. This is the contract
    LiveManager relies on — verified separately from the manager so
    a manager-side bug doesn't mask a session-side regression."""
    loop, _renderer, _ = _make_loop_with_three_slides(tmp_path)
    await loop.start()
    try:
        with patch("openmarquee.live.RTCPeerConnection", _FakeRTCPeerConnection):
            session = LiveSession(loop)
            await session.start_webrtc("v=0\r\noffer\r\n")
            await _wait_until(lambda: loop.is_paused)
            assert loop.is_paused

            await session.close()
            assert not loop.is_paused
    finally:
        await loop.stop()


# --- 5. stream takeover (STREAM/VLC slice 4) -------------------------------


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


def _patch_mock_ffmpeg(
    monkeypatch,
    tmp_path,
    *,
    n_frames: int,
    frame_size: int,
    source_size: tuple[int, int] = (8, 8),
):
    """Point FfmpegStreamSource's StreamConsumer at a mock-ffmpeg
    binary that emits `n_frames` frames of `frame_size` bytes.

    HW-decode (2026-05-20): the consumer ffprobes for the source
    resolution; inject `source_size` so the probe is skipped (the
    probe path has its own coverage). `frame_size` should be the
    NV12 size for `source_size` (src_w*src_h*3//2)."""
    import functools

    from openmarquee.stream_consumer import StreamConsumer
    from tests.test_stream_consumer import _write_mock_ffmpeg

    mock = _write_mock_ffmpeg(tmp_path / "ffmpeg", frame_size=frame_size, n_frames=n_frames)
    monkeypatch.setattr(
        "openmarquee.stream_source.StreamConsumer",
        functools.partial(StreamConsumer, ffmpeg_bin=mock, source_size=source_size),
    )


@pytest.mark.asyncio
async def test_start_stream_session_pulls_frames(tmp_path, monkeypatch):
    """End-to-end: LiveManager.start() with a StreamStartRequest
    spawns an FfmpegStreamSource, pumps the (mock) ffmpeg's frames to
    the renderer, and pauses the playlist. The slice-4 gate.

    HW-decode (2026-05-20): the stream source now produces NV12 frames
    at the source resolution (here 8x8 -> 96-byte NV12)."""
    # NV12 frame size for the injected 8x8 source.
    frame_size = 8 * 8 * 3 // 2
    _patch_mock_ffmpeg(
        monkeypatch,
        tmp_path,
        n_frames=4,
        frame_size=frame_size,
        source_size=(8, 8),
    )
    loop, renderer = _empty_loop(tmp_path)
    await loop.start()
    captured: list[bytes] = []
    captured_formats: list[str] = []
    original_render = renderer.render_frame

    def _record(data, **kwargs):
        captured.append(data)
        captured_formats.append(kwargs.get("pixel_format", "rgb888"))
        return original_render(data, **kwargs)

    renderer.render_frame = _record
    try:
        manager = LiveManager(loop)
        session_id, answer = await manager.start(StreamStartRequest(url="rtsp://laptop:8554/live"))
        # A stream takeover has no SDP answer to hand back.
        assert answer is None
        assert manager.is_active
        assert manager.active_session_id == session_id
        # The takeover paused the playlist.
        assert await _wait_until(lambda: loop.is_paused)
        # The pump drains the mock ffmpeg's 4 frames to the renderer.
        assert await _wait_until(lambda: len(captured) >= 4)
        assert len(captured) == 4
        assert all(len(f) == frame_size for f in captured)
        # HW-decode: the stream source declares NV12; the pump threads
        # that into render_frame().
        assert all(fmt == "nv12" for fmt in captured_formats)
    finally:
        await manager.stop_all()
        await loop.stop()
    # close() resumed playback.
    assert not loop.is_paused


@pytest.mark.asyncio
async def test_stream_session_has_no_peer_connection(tmp_path, monkeypatch):
    """A stream takeover uses no RTCPeerConnection — that machinery is
    WebRTC-only. It DOES arm a watchdog: stream hardening C2 gives the
    stream path a first-frame watchdog symmetric to the WebRTC phantom-
    track one (`_watch_for_first_frame`), so an unreachable URL can't
    freeze the playlist. The watchdog is a stream-path task, not a
    peer-connection; the no-PC invariant is what this test guards."""
    # NV12 frame size for the injected 8x8 source.
    _patch_mock_ffmpeg(
        monkeypatch,
        tmp_path,
        n_frames=1,
        frame_size=8 * 8 * 3 // 2,
        source_size=(8, 8),
    )
    loop, _renderer = _empty_loop(tmp_path)
    await loop.start()
    try:
        session = LiveSession(loop)
        await session.start_stream("rtsp://laptop:8554/live")
        assert session._pc is None
        # C2: the stream path arms a first-frame watchdog.
        assert session._watchdog_task is not None
        await session.close()
        assert not loop.is_paused
    finally:
        await loop.stop()


# --- 6. stream-takeover freeze hardening (C2, findings M1+M2) ---------------


@pytest.mark.asyncio
async def test_stream_takeover_unreachable_url_raises_and_resumes_playlist(tmp_path, monkeypatch):
    """C2 / finding M1 + QA verify-audit 2026-07-15: a stream takeover to a
    dead/unreachable URL — ffmpeg yields ZERO frames — must NOT leave the
    playlist frozen, AND must now fail LOUDLY instead of returning a
    misleading success that self-closes seconds later.

    The honest-start gate (`_await_stream_start`) sees the pump exit with no
    frame and raises StreamSourceUnreachable; LiveManager.start tears the
    session down (resuming the playlist) and re-raises, so the operator gets
    an error rather than a phantom session. (Pre-audit this returned a 200
    and relied on the first-frame watchdog to silently close.)"""
    # The gate returns as soon as the pump exits (FIRST_COMPLETED), so this
    # is fast regardless; compress the window to keep the worst case bounded.
    monkeypatch.setattr(LiveSession, "_STREAM_START_LIVENESS_SECONDS", 2.0)
    # n_frames=0 → the mock ffmpeg emits nothing and exits: the
    # "unreachable URL" failure mode (ffmpeg/ffprobe yields no media).
    _patch_mock_ffmpeg(
        monkeypatch,
        tmp_path,
        n_frames=0,
        frame_size=8 * 8 * 3 // 2,
        source_size=(8, 8),
    )
    loop, _renderer = _empty_loop(tmp_path)
    await loop.start()
    try:
        manager = LiveManager(loop)
        with pytest.raises(StreamSourceUnreachable):
            await manager.start(StreamStartRequest(url="rtsp://dead-host:8554/nothing"))
        # The failed start tore the session down — no phantom session leaked.
        assert not manager.is_active
        assert manager._session is None
        # close() resumed the loop — the playlist is not frozen. resume()
        # runs at the END of close() after a subprocess reap, so poll.
        assert await _wait_until(lambda: not loop.is_paused, timeout=2.0), (
            "an unreachable-URL takeover left the playlist frozen"
        )
    finally:
        await manager.stop_all()
        await loop.stop()


@pytest.mark.asyncio
async def test_stream_takeover_midstream_pump_exit_does_not_freeze_playlist(tmp_path, monkeypatch):
    """C2 / finding M2: a stream takeover that DID render frames and
    then loses its source mid-stream (ffmpeg crashes / disconnects /
    the stream EOFs) must NOT leave the session is_active with the
    playlist frozen on the last frame.

    A mock ffmpeg that emits a few frames then exits stands in for the
    mid-stream-disconnect case (the pump exhausts `frames()` AFTER
    having yielded frames). The pump's `finally`-block auto-close fires
    so the session closes and the playlist resumes — the invariant is
    "a pump that has exited MUST NOT leave the session is_active"."""
    frame_size = 8 * 8 * 3 // 2
    # n_frames=3 → the pump renders 3 frames, then frames() ends:
    # mid-stream pump exit AFTER frames flowed.
    _patch_mock_ffmpeg(
        monkeypatch,
        tmp_path,
        n_frames=3,
        frame_size=frame_size,
        source_size=(8, 8),
    )
    loop, renderer = _empty_loop(tmp_path)
    captured: list[bytes] = []
    original_render = renderer.render_frame

    def _record(data, **kwargs):
        captured.append(data)
        return original_render(data, **kwargs)

    renderer.render_frame = _record
    await loop.start()
    try:
        manager = LiveManager(loop)
        session_id, _answer = await manager.start(StreamStartRequest(url="rtsp://laptop:8554/live"))
        session = manager._session
        assert session is not None and session.id == session_id
        assert await _wait_until(lambda: loop.is_paused)
        # Frames DID flow (this is the mid-stream case, not the
        # never-reachable case).
        assert await _wait_until(lambda: len(captured) >= 3)
        # ...and then frames() ended: the pump exited and MUST have
        # auto-closed the session — not left it is_active.
        assert await _wait_until(lambda: session.closed, timeout=2.0), (
            "session never closed after a mid-stream pump exit — "
            "the playlist froze on the last frame"
        )
        assert session.closed
        assert not manager.is_active
        # close() resumed the loop — poll: resume() runs at the END of
        # close(), after `closed` has already flipped True.
        assert await _wait_until(lambda: not loop.is_paused, timeout=2.0), (
            "close() did not resume the playlist"
        )
    finally:
        await manager.stop_all()
        await loop.stop()


@pytest.mark.asyncio
async def test_stream_takeover_healthy_stream_stays_active(tmp_path, monkeypatch):
    """C2 happy-path guard: a healthy takeover that IS producing frames
    must stay is_active and must NOT be closed by the first-frame
    watchdog. Once frames flow, `_first_frame_event` is set and the
    watchdog disarms — it only fires on the ZERO-frames-ever case.

    A `continuous` mock ffmpeg streams frames endlessly; the watchdog
    timeout is compressed but kept comfortably above mock subprocess
    spawn + first-frame latency, and the test then observes well past
    that timeout — a live, frame-producing session must remain open
    and the playlist paused until an explicit operator stop."""
    # Compressed so the test is fast, but long enough that the mock
    # ffmpeg's spawn + first frame land inside it — the watchdog must
    # see the first frame and disarm. The test waits past this window
    # below: if a healthy session were wrongly killable it'd be caught.
    monkeypatch.setattr(LiveSession, "_PHANTOM_TIMEOUT_SECONDS", 1.0)

    import functools

    from openmarquee.stream_consumer import StreamConsumer
    from tests.test_stream_consumer import _write_mock_ffmpeg

    frame_size = 8 * 8 * 3 // 2
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=frame_size, n_frames=0, continuous=True
    )
    monkeypatch.setattr(
        "openmarquee.stream_source.StreamConsumer",
        functools.partial(StreamConsumer, ffmpeg_bin=mock, source_size=(8, 8)),
    )

    loop, renderer = _empty_loop(tmp_path)
    captured: list[bytes] = []
    original_render = renderer.render_frame

    def _record(data, **kwargs):
        captured.append(data)
        return original_render(data, **kwargs)

    renderer.render_frame = _record
    await loop.start()
    try:
        manager = LiveManager(loop)
        session_id, _answer = await manager.start(StreamStartRequest(url="rtsp://laptop:8554/live"))
        session = manager._session
        assert session is not None and session.id == session_id
        assert await _wait_until(lambda: loop.is_paused)
        # Frames are flowing — the first frame disarms the watchdog.
        assert await _wait_until(lambda: len(captured) >= 2)
        # The first-frame watchdog should have seen the first frame
        # and exited (disarmed), NOT have fired its close().
        assert session._watchdog_task is not None
        assert await _wait_until(lambda: session._watchdog_task.done())
        # Wait well past the watchdog timeout — a healthy, frame-
        # producing session must NOT be auto-closed.
        await asyncio.sleep(1.3)
        assert not session.closed, (
            "the first-frame watchdog wrongly closed a live, frame-producing session"
        )
        assert manager.is_active
        assert loop.is_paused
        # A normal operator-driven stop still works.
        await manager.stop(session_id)
        assert session.closed
        assert not manager.is_active
        assert not loop.is_paused
    finally:
        await manager.stop_all()


# ---- Fork B: pause/resume behavioral coverage ----


@pytest.mark.asyncio
async def test_session_pause_skips_render_frame(tmp_path, monkeypatch):
    """When pause() is set, the pump drains frames from the source
    but skips renderer.render_frame — DRM scanout holds the last
    painted frame, but ffmpeg keeps flowing so it doesn't backpressure
    and resume can pick up at the current live frame."""
    import functools

    from openmarquee.stream_consumer import StreamConsumer
    from tests.test_stream_consumer import _write_mock_ffmpeg

    frame_size = 8 * 8 * 3 // 2  # NV12: 1.5 bytes/pixel
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=frame_size, n_frames=0, continuous=True
    )
    monkeypatch.setattr(
        "openmarquee.stream_source.StreamConsumer",
        functools.partial(StreamConsumer, ffmpeg_bin=mock, source_size=(8, 8)),
    )

    loop, renderer = _empty_loop(tmp_path)
    captured: list[bytes] = []
    original_render = renderer.render_frame

    def _record(data, **kwargs):
        captured.append(data)
        return original_render(data, **kwargs)

    renderer.render_frame = _record
    await loop.start()
    manager = LiveManager(loop)
    try:
        session_id, _answer = await manager.start(StreamStartRequest(url="rtsp://laptop:8554/live"))
        session = manager._session
        assert session is not None and session.id == session_id

        # Wait until the pump is actually rendering frames.
        assert await _wait_until(lambda: len(captured) >= 2)
        baseline_count = len(captured)

        # Pause — subsequent ffmpeg frames must NOT reach the renderer.
        await manager.pause(session_id)
        assert session.paused
        # Wait long enough for several mock-ffmpeg frames to have been
        # yielded; the renderer count must NOT grow.
        await asyncio.sleep(0.4)
        assert len(captured) == baseline_count, (
            f"pause leaked render calls: {len(captured) - baseline_count}"
        )
        # Pin "pump is alive + draining" so the no-render-growth check
        # above isn't satisfied by a pump that silently crashed during
        # pause (subagent review nit — without this, the assertion
        # would falsely pass on a regression that crashed the loop).
        assert not session.closed, "session closed during pause"
        assert session._pump_task is not None
        assert not session._pump_task.done(), "pump task ended during pause"

        # Resume — the pump starts rendering again. New frames arrive
        # at the renderer (we picked up at "now", not a replay of the
        # paused-window frames).
        await manager.resume(session_id)
        assert not session.paused
        assert await _wait_until(lambda: len(captured) > baseline_count)
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_pause_before_first_frame_does_not_trip_watchdog(tmp_path, monkeypatch):
    """Regression: a Pause landing BEFORE the first render on a stream
    takeover MUST NOT let the first-frame watchdog auto-close the session.
    The pump sets _first_frame_event at the TOP of its loop (on frame
    DRAIN), before the pause gate — so a drained-but-not-rendered frame
    still proves source-side liveness and disarms the watchdog. (Subagent
    review flagged the original bug: the pump set the event AFTER the pause
    gate, so a Pause before the first render silently tore down a healthy
    session 10s later.)

    Driven at the LiveSession level with the session PRE-PAUSED, because the
    honest-start gate (_await_stream_start, 2026-07-15) now guarantees a
    frame is drained — event set — before manager.start() returns, so
    "pause before the first frame" can't be staged through the manager. If
    the event-set regressed back below the pause gate, a pre-paused source
    would never set it: start_stream's gate would time out and this test's
    is_set() assertion would fail. That's the guard."""
    import functools

    from openmarquee.stream_consumer import StreamConsumer
    from tests.test_stream_consumer import _write_mock_ffmpeg

    # Leave _PHANTOM_TIMEOUT_SECONDS at its default: a healthy source sets the
    # proof-of-life event within milliseconds, disarming the watchdog long
    # before it could fire — so no compression is needed, and none of the
    # flakiness of a tight watchdog racing the mock's subprocess spawn. The
    # honest-start liveness window bounds regression detection instead: a
    # comfortable 3s (vs. the mock's ~30ms emit) so a loaded CI runner can't
    # spuriously time it out on the healthy path, while the regression path
    # (event never set) still fails quickly.
    monkeypatch.setattr(LiveSession, "_STREAM_START_LIVENESS_SECONDS", 3.0)
    frame_size = 8 * 8 * 3 // 2
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=frame_size, n_frames=0, continuous=True
    )
    monkeypatch.setattr(
        "openmarquee.stream_source.StreamConsumer",
        functools.partial(StreamConsumer, ffmpeg_bin=mock, source_size=(8, 8)),
    )

    loop, _renderer = _empty_loop(tmp_path)
    await loop.start()
    manager = LiveManager(loop)
    try:
        session = LiveSession(loop)
        # Pause BEFORE the transport starts: the pump will DRAIN frames but
        # skip render_frame. The drained frame must STILL set the event.
        await session.pause()
        assert session.paused
        await session.start_stream("rtsp://laptop:8554/live")
        manager._session = session  # register so stop_all() tears it down
        # The drained (unrendered) frame set the proof-of-life event...
        assert session._first_frame_event.is_set()
        # ...so the watchdog disarmed (its wait returned) and did NOT close
        # the paused-but-alive session.
        assert session._watchdog_task is not None
        assert await _wait_until(lambda: session._watchdog_task.done())
        await asyncio.sleep(0.3)
        assert not session.closed, "first-frame watchdog wrongly closed a paused-but-alive session"
        assert session.paused
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_live_pump_feeds_the_systemd_watchdog(tmp_path, monkeypatch):
    """Regression (QA JasonsSign1 live-fire 2026-07-15): a live takeover
    PAUSES the playback loop, whose per-advance sd_notify.notify_watchdog()
    ping is the backend's ONLY systemd WATCHDOG=1 keepalive. So a sustained
    live session fed NOTHING to systemd and got WatchdogSec-killed (~2min
    into the no-ping window). The live pump — the active render pipeline
    during a session — must feed the watchdog itself while the loop is
    paused. Fail-before: pre-fix the live pump never pinged, so `pings`
    stays empty after the clear and this times out."""
    import functools

    from openmarquee import sd_notify
    from openmarquee.stream_consumer import StreamConsumer
    from tests.test_stream_consumer import _write_mock_ffmpeg

    pings: list[int] = []
    # Patch the module attr the live pump resolves at call time (bypasses the
    # 1Hz throttle so every pump frame is observable here).
    monkeypatch.setattr(sd_notify, "notify_watchdog", lambda: pings.append(1))

    frame_size = 8 * 8 * 3 // 2
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=frame_size, n_frames=0, continuous=True
    )
    monkeypatch.setattr(
        "openmarquee.stream_source.StreamConsumer",
        functools.partial(StreamConsumer, ffmpeg_bin=mock, source_size=(8, 8)),
    )
    loop, _renderer = _empty_loop(tmp_path)
    await loop.start()
    manager = LiveManager(loop)
    try:
        await manager.start(StreamStartRequest(url="rtsp://laptop:8554/live"))
        # The takeover paused the playback loop — so from here the ONLY thing
        # that can ping the watchdog is the live pump.
        assert await _wait_until(lambda: loop.is_paused)
        # Reset, then require pings that arrive AFTER the playback loop is
        # confirmed paused — so they can ONLY be the live pump's. (This
        # fixture's playlist is empty, so the playback loop never advances +
        # never pings anyway; the clear just drops the pump's own pre-pause
        # early pings so provenance is unambiguous.)
        pings.clear()
        # The live pump renders the continuous source's frames → it must keep
        # feeding the watchdog while the playback loop stays paused.
        assert await _wait_until(lambda: len(pings) >= 1, timeout=2.0), (
            "live pump did not feed the systemd watchdog while the playback "
            "loop was paused — a sustained live session would be WatchdogSec-killed"
        )
    finally:
        await manager.stop_all()
        await loop.stop()


@pytest.mark.asyncio
async def test_session_pause_resume_are_idempotent(tmp_path):
    """Repeated pause/resume on the same session is a no-op without
    error. Tests against a fresh session without a started transport
    (no source needed — pause/resume only flip a flag)."""
    loop, _renderer = _empty_loop(tmp_path)
    await loop.start()
    manager = LiveManager(loop)
    try:
        from openmarquee.live import LiveSession

        # Build a session directly so we don't need a transport for
        # this pure-flag test. The flag's existence + idempotency is
        # what we're pinning here.
        session = LiveSession(loop)
        assert not session.paused
        await session.pause()
        assert session.paused
        await session.pause()  # idempotent
        assert session.paused
        await session.resume()
        assert not session.paused
        await session.resume()  # idempotent
        assert not session.paused
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_manager_pause_raises_live_not_active_on_wrong_id(tmp_path):
    """Wrong session_id MUST raise LiveNotActive — prevents one
    phone from pausing another phone's session by guessing an id."""
    from uuid import uuid4

    from openmarquee.live import LiveNotActive

    loop, _renderer = _empty_loop(tmp_path)
    await loop.start()
    manager = LiveManager(loop)
    try:
        # No session active.
        with pytest.raises(LiveNotActive):
            await manager.pause(uuid4())
        with pytest.raises(LiveNotActive):
            await manager.resume(uuid4())
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_manager_is_active_and_paused_property(tmp_path):
    """The `is_active_and_paused` property combines session-presence
    and paused-flag for /api/live/status.paused. Idle → False;
    active+unpaused → False; active+paused → True."""
    loop, _renderer = _empty_loop(tmp_path)
    await loop.start()
    manager = LiveManager(loop)
    try:
        assert manager.is_active_and_paused is False

        from openmarquee.live import LiveSession

        session = LiveSession(loop)
        manager._session = session  # bypass the lock for this pure-flag test
        assert manager.is_active_and_paused is False  # active but not paused
        await session.pause()
        assert manager.is_active_and_paused is True
        await session.resume()
        assert manager.is_active_and_paused is False
    finally:
        await manager.stop_all()
        await loop.stop()


# ---- 2026-05-24 D3 Option C: RTCPeerConnection.connectionState ----


@pytest.mark.asyncio
async def test_connection_state_failed_triggers_close(tmp_path, monkeypatch):
    """Primary failure detector (audit D3 Option C). When aiortc's
    connectionState transitions to `failed` (ICE failure, DTLS failure,
    mid-stream transport collapse), the on-state-change callback must
    close the session immediately rather than waiting for the 30s
    safety-net timeout."""
    # Bump safety-net high enough that ONLY the connectionstatechange
    # path can possibly fire in this test's window.
    monkeypatch.setattr(LiveSession, "_PHANTOM_TIMEOUT_SECONDS_WEBRTC", 60.0)
    loop, _renderer, _ = _make_loop_with_three_slides(tmp_path)
    await loop.start()
    try:
        with patch("openmarquee.live.RTCPeerConnection", _FakeRTCPeerConnection):
            session = LiveSession(loop)
            await session.start_webrtc("v=0\r\noffer\r\n")
            assert not session.closed
            # Simulate ICE/DTLS failure: mutate fake state + fire callback.
            session._pc.connectionState = "failed"
            session._pc.handlers["connectionstatechange"]()
            # The callback schedules close() as an async task; yield to
            # the event loop so it runs.
            await _wait_until(lambda: session.closed, timeout=1.0)
            assert session.closed
            assert not loop.is_paused, "close() should have resumed playback"
    finally:
        await loop.stop()


@pytest.mark.asyncio
async def test_connection_state_disconnected_triggers_close(tmp_path, monkeypatch):
    """`disconnected` is also terminal per the audit choice (qarl
    Option C). WebRTC spec allows `disconnected` to recover to
    `connected`, but openMarquee bias is fail-fast so the operator
    sees the session end + can re-takeover rather than waiting on a
    flapping link to recover."""
    monkeypatch.setattr(LiveSession, "_PHANTOM_TIMEOUT_SECONDS_WEBRTC", 60.0)
    loop, _renderer, _ = _make_loop_with_three_slides(tmp_path)
    await loop.start()
    try:
        with patch("openmarquee.live.RTCPeerConnection", _FakeRTCPeerConnection):
            session = LiveSession(loop)
            await session.start_webrtc("v=0\r\noffer\r\n")
            session._pc.connectionState = "disconnected"
            session._pc.handlers["connectionstatechange"]()
            await _wait_until(lambda: session.closed, timeout=1.0)
            assert session.closed
    finally:
        await loop.stop()


@pytest.mark.asyncio
async def test_connection_state_healthy_transitions_do_not_close(tmp_path, monkeypatch):
    """`new` -> `connecting` -> `connected` is the happy path. The
    callback must NOT close the session on any non-terminal state;
    a future spec-tweak that adds new state names (e.g. `relaying`)
    should also not accidentally close."""
    monkeypatch.setattr(LiveSession, "_PHANTOM_TIMEOUT_SECONDS_WEBRTC", 60.0)
    loop, _renderer, _ = _make_loop_with_three_slides(tmp_path)
    await loop.start()
    try:
        with patch("openmarquee.live.RTCPeerConnection", _FakeRTCPeerConnection):
            session = LiveSession(loop)
            await session.start_webrtc("v=0\r\noffer\r\n")
            handler = session._pc.handlers["connectionstatechange"]
            for healthy in ("connecting", "connected", "new"):
                session._pc.connectionState = healthy
                handler()
                # Yield once to let any spuriously-scheduled close() task run.
                await asyncio.sleep(0)
                assert not session.closed, f"healthy state {healthy!r} must not trigger close()"
            # Close manually so the loop teardown cleans up.
            await session.close()
    finally:
        await loop.stop()


def test_phantom_timeout_constants_split_webrtc_vs_stream():
    """Regression-lock on the split-constant design (subagent caught
    this pre-commit). The WebRTC safety-net and the stream first-frame
    watchdog are SEPARATE constants — a single shared one would couple two
    independently-tuned budgets (the WebRTC path has a fast secondary
    detector via connectionstatechange; the stream path has none).

    QA verify-audit 2026-07-15: the stream watchdog default was bumped
    10s -> 75s (and made env-tunable) to sit strictly above the 60s ffprobe
    budget after probing on the Pi Zero 2 W was found to exceed the old
    8s/10s budget on VALID sources; the WebRTC safety-net (30s) is unrelated
    and unchanged. The two must stay distinct — the stream one is no longer
    the smaller of the pair."""
    assert LiveSession._PHANTOM_TIMEOUT_SECONDS_WEBRTC == 30.0
    assert LiveSession._PHANTOM_TIMEOUT_SECONDS == 75.0
    assert LiveSession._PHANTOM_TIMEOUT_SECONDS != LiveSession._PHANTOM_TIMEOUT_SECONDS_WEBRTC


def test_stream_timing_budgets_are_env_tunable(monkeypatch):
    """QA verify-audit 2026-07-15: the honest-start liveness window has a
    short default (so a valid-but-slow probe doesn't hang the request past
    it) and, like the first-frame watchdog, is env-tunable. Pins the
    default + the helper's parse rules (valid override wins; junk / non-
    positive / unset fall back)."""
    from openmarquee import live as live_mod

    assert LiveSession._STREAM_START_LIVENESS_SECONDS == 5.0
    monkeypatch.setenv("X_TEST_LIVENESS", "3.5")
    assert live_mod._env_positive_float("X_TEST_LIVENESS", 5.0) == 3.5
    monkeypatch.setenv("X_TEST_LIVENESS", "junk")
    assert live_mod._env_positive_float("X_TEST_LIVENESS", 5.0) == 5.0
    monkeypatch.setenv("X_TEST_LIVENESS", "0")
    assert live_mod._env_positive_float("X_TEST_LIVENESS", 5.0) == 5.0
    monkeypatch.delenv("X_TEST_LIVENESS", raising=False)
    assert live_mod._env_positive_float("X_TEST_LIVENESS", 5.0) == 5.0
