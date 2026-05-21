"""Stream takeover (SYSTEM_SPEC §5.11 + docs/STREAM_VLC_PROPOSAL.md).

A live source preempts the active playlist on the device's screen.
Two transports:

- WebRTC — a phone publishes its camera via aiortc (no Chromium on
  the device). SDP is negotiated in one round trip.
- RTSP — the operator's VLC publishes an RTSP stream; the Pi pulls
  it with ffmpeg (the slice-2 VlcRtspConsumer).

Either way the decoded frames flow through the renderer wire format
(§7.6); the playback loop pauses while a session is active and
resumes the same slide it was on when the session ends.

Two classes:

- StreamManager — process-singleton holding at most one StreamSession
  at a time. /api/stream/start refuses if one's already active;
  /api/stream/takeover force-stops + restarts. start()/takeover()
  dispatch on the request's `kind`.

- StreamSession — one takeover. It owns a StreamSource and a pump
  task draining it to the renderer; the WebRTC transport additionally
  owns an RTCPeerConnection + a phantom-track watchdog. close() tears
  down whichever transport is in use and resumes the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Literal, Union
from uuid import UUID, uuid4

from aiortc import RTCPeerConnection, RTCSessionDescription
from pydantic import BaseModel

from openmarquee.playback import PlaybackLoop
from openmarquee.stream_source import RtspStreamSource, WebRtcStreamSource

log = logging.getLogger(__name__)


class WebRtcStartRequest(BaseModel):
    """Start a phone-camera takeover. `sdp_offer` is the phone's
    WebRTC offer; v1 carries video only (audio: false at
    getUserMedia, §5.11)."""

    kind: Literal["webrtc"] = "webrtc"
    sdp_offer: str


class RtspStartRequest(BaseModel):
    """Start a VLC takeover. `url` is the RTSP URL the operator's VLC
    is publishing (e.g. rtsp://laptop:8554/live)."""

    kind: Literal["rtsp"] = "rtsp"
    url: str


# The /api/stream/start + /takeover request body. A plain (non-
# discriminated) union, so Pydantic's smart matching applies: each
# variant's `kind` has a default, which means a legacy body with no
# `kind` ({"sdp_offer": ...}) still validates as a WebRtcStartRequest
# — the deployed phone client predates the VLC work and must keep
# working until its UI bundle is refreshed.
StreamStartRequest = Union[WebRtcStartRequest, RtspStartRequest]


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
    """One takeover feeding the playback engine.

    The session owns a `StreamSource` and a pump task that drains it
    to the renderer. Two transports:

    - `start_webrtc()` — a phone-camera takeover: negotiate SDP on an
      RTCPeerConnection, wrap the inbound track in a
      `WebRtcStreamSource`, and arm a phantom-track watchdog.
    - `start_rtsp()` — a VLC takeover: wrap an `RtspStreamSource`
      (ffmpeg pulling the RTSP URL); no peer connection, no SDP, no
      watchdog.

    Either way the source yields RGB888 frames cover-fit-scaled to
    the renderer's native dimensions and the pump hands each to
    `renderer.render_frame` — same wire format the playlist source
    uses (§7.6). close() tears down whichever transport is in use.
    """

    # Phase 12.1 Finding #2 mitigation — phantom-session watchdog
    # timeout (WebRTC transport only). If no on_track event fires
    # within this window after start_webrtc() returns, the session is
    # auto-closed. Catches
    # bogus SDPs that parse + answer cleanly but never deliver a
    # real media track, plus phones that crash mid-handshake. Set
    # comfortably above the worst-case real-network handshake time
    # (~1-2s on local Tailnet, ~5s on slow/relay paths); 10s is the
    # same threshold §5.11 uses for the PC-disconnect timeout, so
    # the two paths converge on the same UX.
    #
    # TODO(qarl-confirm): default mitigation is timeout-based.
    # Alternative #1: pre-validate SDP at the API boundary (parse
    # the m= sections, require a video media line). Pro: rejects
    # at /api/stream/start before any session exists. Con: needs
    # an SDP parser, can miss subtle malformed cases that aiortc
    # would still accept-but-not-negotiate.
    # Alternative #2: poll RTCPeerConnection.connectionState every
    # second; close on "failed" / "disconnected". Pro: catches
    # mid-stream drops too. Con: aiortc's connectionState semantics
    # vary across versions.
    # Combination of all three is also possible. Flip if QA finds
    # the timeout-only approach lets a class of bogus-SDP phantoms
    # through.
    _PHANTOM_TIMEOUT_SECONDS = 10.0

    def __init__(self, playback: PlaybackLoop):
        self._playback = playback
        self.id: UUID = uuid4()
        # Wall-clock timestamp at session creation. The Stream UI's
        # Elapsed metric ticks against (now - started_at) read off
        # /api/stream/status, so the value survives a panel re-mount
        # mid-stream (Phase A.2 — closes the loop on QA's A.2 callout).
        # UTC explicit so the JSON response is unambiguous; the phone
        # subtracts wall-clock-now in the same UTC frame.
        self.started_at: datetime = datetime.now(UTC)
        # The frame source + pump are created by whichever start_*
        # transport method runs.
        self._source: WebRtcStreamSource | RtspStreamSource | None = None
        self._pump_task: asyncio.Task | None = None
        self._closed = False
        # WebRTC-transport-only state (stays None for an RTSP session).
        self._pc: RTCPeerConnection | None = None
        # Phase 12.1 Finding #2: signaled when the first on_track
        # event fires. The phantom-session watchdog awaits this with
        # a timeout; if it never resolves, the session was never
        # backed by real media and the watchdog auto-closes.
        self._first_track_event = asyncio.Event()
        self._watchdog_task: asyncio.Task | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    async def start_webrtc(self, sdp_offer: str) -> str:
        """WebRTC takeover: set the remote offer, create an answer,
        pause playback.

        Returns the SDP answer (with ICE candidates baked in — non-trickle
        for v1 per §5.11). The caller hands this back to the phone in the
        same /api/stream/start response.
        """
        self._pc = RTCPeerConnection()
        source = WebRtcStreamSource(self._playback.renderer)
        self._source = source
        offer = RTCSessionDescription(sdp=sdp_offer, type="offer")

        @self._pc.on("track")
        def on_track(track):  # noqa: ANN001 — aiortc's MediaStreamTrack
            # Only video for v1 — audio is muted at capture per §5.11.
            if track.kind == "video":
                self._first_track_event.set()
                source.set_track(track)
                self._pump_task = asyncio.create_task(self._pump())

        await self._pc.setRemoteDescription(offer)
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)
        # Pause playback now that the negotiation is complete; the first
        # video frame may arrive any moment via the on_track callback.
        await self._playback.pause()
        # Phase 12.1 Finding #2 phantom-session watchdog. Schedule a
        # background task that waits up to _PHANTOM_TIMEOUT_SECONDS
        # for the first track event; if no track materializes (bogus
        # SDP that answered cleanly but had no real media, phone
        # crashed mid-handshake, etc.) the session is auto-closed.
        # Closing flips _closed=True, which makes StreamManager.
        # is_active return False on the next /status query — the
        # phone will see the session has gone away.
        self._watchdog_task = asyncio.create_task(self._watch_for_first_track())
        # setLocalDescription may rewrite the SDP with gathered ICE
        # candidates; read the canonical form back from the PC.
        return self._pc.localDescription.sdp

    async def start_rtsp(self, rtsp_url: str) -> None:
        """VLC takeover: pause playback and start pumping the RTSP
        source.

        There is no SDP, no peer connection, and no phantom-track
        watchdog — ffmpeg either delivers frames or it doesn't, and
        the operator sees the result on glass. The pump starts
        immediately (unlike WebRTC, there is no track to wait for).
        """
        self._source = RtspStreamSource(self._playback.renderer, rtsp_url)
        await self._playback.pause()
        self._pump_task = asyncio.create_task(self._pump())

    async def _watch_for_first_track(self) -> None:
        """Phase 12.1 Finding #2: auto-close if no track materializes
        within _PHANTOM_TIMEOUT_SECONDS. Cancellation-safe: close()
        cancels this task, which surfaces as CancelledError here and
        is silently re-raised so the cancel completes."""
        try:
            await asyncio.wait_for(
                self._first_track_event.wait(),
                timeout=self._PHANTOM_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            if self._closed:
                # Session was closed via the normal path during the
                # wait — nothing to do.
                return
            log.warning(
                "stream: session %s saw no track within %.1fs; closing as phantom",
                self.id,
                self._PHANTOM_TIMEOUT_SECONDS,
            )
            await self.close()
        except asyncio.CancelledError:
            raise

    async def _pump(self) -> None:
        """Drive the source: push each yielded frame to the renderer.

        Runs until the source's iterator is exhausted (track ended,
        session closed) or the task is cancelled. Decode/scale errors
        are handled inside the source.

        If `render_frame` raises, the pump logs ONCE and stops. A
        renderer rejecting a pushed frame is not a transient
        per-frame glitch — it means the renderer cannot accept
        push-frame rendering at all (a Rust sidecar without the
        push-frames IPC op). Logging per frame at the stream's frame
        rate floods the journal — that per-frame-logging shape caused
        a measured fps regression on the production sign. The session
        stays "active" holding the last frame; close() still tears it
        down normally.
        """
        renderer = self._playback.renderer
        source = self._source
        assert source is not None  # _pump is only spawned with a source
        # HW-decode (2026-05-20): the source declares its pixel format.
        # An NV12 source (RtspStreamSource) yields source-resolution
        # frames; the renderer needs the source dims for its GPU cover-
        # fit. An RGB888 source (WebRtcStreamSource) needs neither —
        # render_frame()'s rgb888 default uses the panel dims.
        pixel_format = getattr(source, "pixel_format", "rgb888")
        try:
            async for frame_bytes in source.frames():
                frame_w = frame_h = None
                if pixel_format == "nv12":
                    # frame_dims() is known once the consumer's ffprobe
                    # has run — which is before its first frame, so this
                    # is populated by the time we get here.
                    dims = source.frame_dims() if hasattr(source, "frame_dims") else None
                    if dims is None:
                        log.error(
                            "stream: NV12 source yielded a frame before "
                            "its source dimensions were known; stopping "
                            "the frame pump."
                        )
                        return
                    frame_w, frame_h = dims
                try:
                    renderer.render_frame(
                        frame_bytes,
                        pixel_format=pixel_format,
                        frame_w=frame_w,
                        frame_h=frame_h,
                    )
                except Exception:
                    log.error(
                        "stream: renderer rejected a pushed frame — "
                        "push-frame rendering is unavailable; stopping "
                        "the frame pump. The takeover session stays "
                        "active holding the last frame."
                    )
                    return
        except asyncio.CancelledError:
            raise
        finally:
            # STREAM/VLC slice 2.5: end the sidecar's frame-pump
            # session on EVERY exit path (track end, render failure,
            # cancellation) so the sidecar can't hang blocked in
            # pump-mode waiting for a frame that will never come.
            try:
                renderer.end_external_frames()
            except Exception:
                log.exception("stream: end_external_frames failed")

    async def close(self) -> None:
        """Tear down whichever transport is in use and resume the
        playback loop. Idempotent; None-safe if no transport ever
        started (e.g. a start_* that raised before wiring anything)."""
        if self._closed:
            return
        self._closed = True
        # Cancel the phantom-session watchdog if it's still pending —
        # the session is closing via the normal path, so the watchdog's
        # timeout-fire path doesn't need to do its own close. Skipped
        # when the watchdog is the caller (TimeoutError path → close()
        # → here), since that task is itself currently running and
        # cancelling it would self-cancel awkwardly. Self-cancel is
        # detected via `asyncio.current_task()`. (RTSP sessions have
        # no watchdog — _watchdog_task stays None.)
        if (
            self._watchdog_task is not None
            and not self._watchdog_task.done()
            and asyncio.current_task() is not self._watchdog_task
        ):
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._watchdog_task
        # Close the source first so its frames() iterator stops, then
        # cancel the pump in case it is blocked mid-read inside the
        # source's iterator (close() alone can't interrupt a pending
        # track.recv() / ffmpeg stdout read).
        if self._source is not None:
            await self._source.close()
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump_task
        try:
            # The RTCPeerConnection exists only for a WebRTC session.
            if self._pc is not None:
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

    @property
    def active_session_started_at(self) -> datetime | None:
        """Wall-clock timestamp the active session was created.

        Surfaced through /api/stream/status so the publishing phone's
        Elapsed counter ticks against the device's authoritative start
        time — survives a panel re-mount and is correct even if the
        phone's clock is skewed from the device's. None when no
        session is active.
        """
        return self._session.started_at if self.is_active else None

    @staticmethod
    async def _start_session(
        session: StreamSession, request: StreamStartRequest
    ) -> str | None:
        """Run the transport-specific start for `request`'s kind.

        Returns the SDP answer for a WebRTC start, or None for an RTSP
        start (there is no answer to hand back)."""
        if isinstance(request, WebRtcStartRequest):
            return await session.start_webrtc(request.sdp_offer)
        return await session.start_rtsp(request.url)

    async def start(
        self, request: StreamStartRequest
    ) -> tuple[UUID, str | None]:
        """Negotiate a new session. Returns (session_id, sdp_answer);
        sdp_answer is None for an RTSP start.

        Raises StreamAlreadyActive if a session is already running —
        the phone should switch to the take-over affordance.
        """
        async with self._lock:
            if self.is_active:
                assert self._session is not None
                raise StreamAlreadyActive(self._session.id)
            session = StreamSession(self._playback)
            try:
                answer_sdp = await self._start_session(session, request)
            except Exception:
                # Start failed — make sure we don't leak a half-open
                # transport and that playback resumes if pause fired.
                await session.close()
                raise
            self._session = session
            return (session.id, answer_sdp)

    async def takeover(
        self, request: StreamStartRequest
    ) -> tuple[UUID, str | None]:
        """Force-stop any active session, then start a new one.

        No-op-but-still-creates if nothing was active — the user got to
        Take Over slightly after the prior session ended naturally.
        """
        async with self._lock:
            if self._session is not None:
                await self._stop_locked()
            session = StreamSession(self._playback)
            try:
                answer_sdp = await self._start_session(session, request)
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
