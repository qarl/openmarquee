"""WebRTC stream takeover (SYSTEM_SPEC §5.11).

A single phone publishes its camera live to the device's screen,
preempting the active playlist. The WebRTC subscriber lives in the
Python playback engine via aiortc — no Chromium on the device.
Decoded video frames flow through the existing renderer wire format
(§7.6); the playback loop pauses while a session is active and
resumes the same slide it was on when the session ends.

Two classes:

- StreamManager — process-singleton holding at most one StreamSession
  at a time. /api/stream/start refuses if one's already active;
  /api/stream/takeover force-stops + restarts.

- StreamSession — one RTCPeerConnection. Lifecycle: start(offer)
  negotiates SDP and pauses the playback loop, then incoming video
  frames are scaled and pushed to the renderer; close() tears down
  the PC and resumes the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from uuid import UUID, uuid4

from aiortc import RTCPeerConnection, RTCSessionDescription
from PIL import Image

from openmarquee.playback import PlaybackLoop, _cover_fit

log = logging.getLogger(__name__)


class StreamAlreadyActive(Exception):
    """Raised when /api/stream/start is called with a session active.

    The route catches this and returns 409 with the active session id,
    so the phone can surface a "take over" affordance.
    """

    def __init__(self, active_session_id: UUID):
        super().__init__(f"stream session {active_session_id} is already active")
        self.active_session_id = active_session_id


class StreamNotActive(Exception):
    """Raised when /api/stream/stop is called for an unknown session id.

    Either nothing is active, or a different phone owns the active
    session. The route returns 404.
    """

    def __init__(self, session_id: UUID):
        super().__init__(f"no active stream session {session_id}")
        self.session_id = session_id


class StreamSession:
    """One WebRTC peer connection feeding the playback engine.

    The session owns its RTCPeerConnection and the consumer task that
    pulls decoded frames off the inbound video track. Frames are
    cover-fit-downscaled to the renderer's native dimensions and
    pushed as RGB888 — same wire format the playlist source uses
    (§7.6 renderer wire format).
    """

    def __init__(self, playback: PlaybackLoop):
        self._playback = playback
        self.id: UUID = uuid4()
        self._pc = RTCPeerConnection()
        self._consume_task: asyncio.Task | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self, sdp_offer: str) -> str:
        """Set the remote offer, create an answer, pause playback.

        Returns the SDP answer (with ICE candidates baked in — non-trickle
        for v1 per §5.11). The caller hands this back to the phone in the
        same /api/stream/start response.
        """
        offer = RTCSessionDescription(sdp=sdp_offer, type="offer")

        @self._pc.on("track")
        def on_track(track):  # noqa: ANN001 — aiortc's MediaStreamTrack
            # Only video for v1 — audio is muted at capture per §5.11.
            if track.kind == "video":
                self._consume_task = asyncio.create_task(self._consume_video(track))

        await self._pc.setRemoteDescription(offer)
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)
        # Pause playback now that the negotiation is complete; the first
        # video frame may arrive any moment via the on_track callback.
        await self._playback.pause()
        # setLocalDescription may rewrite the SDP with gathered ICE
        # candidates; read the canonical form back from the PC.
        return self._pc.localDescription.sdp

    async def _consume_video(self, track) -> None:  # noqa: ANN001
        """Pull frames off the track, scale, push to the renderer.

        Runs until the track ends, the session is closed, or a fatal
        decode error occurs. Per-frame failures (renderer crash, scale
        glitch) are logged and skipped — one bad frame doesn't kill
        the takeover.
        """
        renderer = self._playback.renderer
        try:
            while not self._closed:
                frame = await track.recv()  # av.VideoFrame
                target_w = renderer.width
                target_h = renderer.height
                try:
                    rgb = frame.to_ndarray(format="rgb24")
                    pil = Image.fromarray(rgb)
                    if pil.size != (target_w, target_h):
                        pil = _cover_fit(pil, target_w, target_h)
                    renderer.render_frame(pil.tobytes())
                except Exception:
                    log.exception("stream: dropped frame")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Track ended (MediaStreamError) or aiortc raised on recv.
            # Log + return; close() handles cleanup.
            log.info("stream: video track consumer exiting")

    async def close(self) -> None:
        """Tear down the PC and resume the playback loop. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._consume_task is not None and not self._consume_task.done():
            self._consume_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._consume_task
        try:
            await self._pc.close()
        finally:
            # Resume playback even if PC close raised — the alternative
            # is leaving the loop paused forever, which is worse than
            # whatever the close failure was.
            await self._playback.resume()


class StreamManager:
    """Single-publisher state. Owns at most one StreamSession at a time.

    Process-singleton in production (wired in dependencies.py); tests
    instantiate one per fixture.
    """

    def __init__(self, playback: PlaybackLoop):
        self._playback = playback
        self._session: StreamSession | None = None
        # Serializes start/stop/takeover so two concurrent phone requests
        # can't end up with two live sessions or a half-closed one.
        self._lock = asyncio.Lock()

    @property
    def is_active(self) -> bool:
        return self._session is not None and not self._session.closed

    @property
    def active_session_id(self) -> UUID | None:
        return self._session.id if self.is_active else None

    async def start(self, sdp_offer: str) -> tuple[UUID, str]:
        """Negotiate a new session. Returns (session_id, sdp_answer).

        Raises StreamAlreadyActive if a session is already running —
        the phone should switch to the take-over affordance.
        """
        async with self._lock:
            if self.is_active:
                assert self._session is not None
                raise StreamAlreadyActive(self._session.id)
            session = StreamSession(self._playback)
            try:
                answer_sdp = await session.start(sdp_offer)
            except Exception:
                # Negotiation failed — make sure we don't leak a half-open
                # PC and that playback resumes if pause already fired.
                await session.close()
                raise
            self._session = session
            return (session.id, answer_sdp)

    async def takeover(self, sdp_offer: str) -> tuple[UUID, str]:
        """Force-stop any active session, then start a new one.

        No-op-but-still-creates if nothing was active — the user got to
        Take Over slightly after the prior session ended naturally.
        """
        async with self._lock:
            if self._session is not None:
                await self._stop_locked()
            session = StreamSession(self._playback)
            try:
                answer_sdp = await session.start(sdp_offer)
            except Exception:
                await session.close()
                raise
            self._session = session
            return (session.id, answer_sdp)

    async def stop(self, session_id: UUID) -> None:
        """Stop the named session. Raises StreamNotActive if it isn't
        the currently-active session (or nothing is active)."""
        async with self._lock:
            if self._session is None or self._session.id != session_id:
                raise StreamNotActive(session_id)
            await self._stop_locked()

    async def stop_all(self) -> None:
        """Tear down whatever's active. Used at app shutdown so the
        playback loop's resume() fires before we cancel the loop's task."""
        async with self._lock:
            if self._session is not None:
                await self._stop_locked()

    async def _stop_locked(self) -> None:
        """Caller must hold self._lock."""
        if self._session is None:
            return
        try:
            await self._session.close()
        finally:
            self._session = None
