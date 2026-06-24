"""Live takeover (SYSTEM_SPEC §5.11 + docs/STREAM_VLC_PROPOSAL.md).

A live source preempts the active playlist on the device's screen.
Two transports:

- WebRTC — a phone publishes its camera via aiortc (no Chromium on
  the device). SDP is negotiated in one round trip.
- Stream — the operator publishes a network stream (RTSP, RTMP, HLS,
  …); the Pi pulls it with ffmpeg (the slice-2 StreamConsumer).

Either way the decoded frames flow through the renderer wire format
(§7.6); the playback loop pauses while a session is active and
resumes the same slide it was on when the session ends.

Two classes:

- LiveManager — process-singleton holding at most one LiveSession
  at a time. /api/live/start refuses if one's already active;
  /api/live/takeover force-stops + restarts. start()/takeover()
  dispatch on the request's `kind`.

- LiveSession — one takeover. It owns a StreamSource and a pump
  task draining it to the renderer; the WebRTC transport additionally
  owns an RTCPeerConnection + a phantom-track watchdog. close() tears
  down whichever transport is in use and resumes the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel

# LEVER 2 lazy-imports (2026-06-24): aiortc + its transitive deps
# (aioice, pylibsrtp, av/ffmpeg) plus PIL (via stream_source) sum to
# ~10-20 MB of RSS that previously loaded at backend startup just to
# satisfy these module-level imports. Live takeover is operator-
# triggered, so on a prod device (DISABLE_DEV=1 + no Live use) those
# bytes paid no rent.
#
# We use PEP 562 module-level `__getattr__` (see bottom of file) to
# lazy-resolve the heavy symbols on FIRST access AT MODULE LEVEL.
# That preserves test seams (every test patches
# `openmarquee.live.RTCPeerConnection`) — on first patch lookup the
# attr is resolved + cached; subsequent accesses go straight to the
# module dict. start_webrtc() / start_stream() reference the
# names as bare globals, so they pick up either the lazy-resolved
# real class OR a mock injected by tests, transparently.
#
# Module-load-time symbols (LiveAlreadyActive, LiveNotActive, LiveManager,
# LiveStartBody, LiveSession) stay top-level for api_live.py's import.

from openmarquee.playback import PlaybackLoop

log = logging.getLogger(__name__)


class WebRtcStartRequest(BaseModel):
    """Start a phone-camera takeover. `sdp_offer` is the phone's
    WebRTC offer; v1 carries video only (audio: false at
    getUserMedia, §5.11)."""

    kind: Literal["webrtc"] = "webrtc"
    sdp_offer: str


class StreamStartRequest(BaseModel):
    """Start a stream-transport takeover. `url` is the network stream
    URL the operator is publishing (e.g. rtsp://laptop:8554/live).

    Named for the TRANSPORT (a network stream), not the panel — the
    same body shape is reused inside the live-takeover panel."""

    kind: Literal["stream"] = "stream"
    url: str


# The /api/live/start + /takeover request body. A plain (non-
# discriminated) union, so Pydantic's smart matching applies: each
# variant's `kind` has a default, which means a legacy body with no
# `kind` ({"sdp_offer": ...}) still validates as a WebRtcStartRequest
# — the deployed phone client predates the stream-transport work and
# must keep working until its UI bundle is refreshed.
LiveStartBody = WebRtcStartRequest | StreamStartRequest


class LiveAlreadyActive(Exception):
    """Raised when /api/live/start is called with a session active.

    The route catches this and returns 409 with the active session id,
    so the phone can surface a "take over" affordance.
    """

    def __init__(self, active_session_id: UUID):
        super().__init__(f"live session {active_session_id} is already active")
        self.active_session_id = active_session_id


class LiveNotActive(Exception):
    """Raised when /api/live/stop is called for an unknown session id.

    Either nothing is active, or a different phone owns the active
    session. The route returns 404.
    """

    def __init__(self, session_id: UUID):
        super().__init__(f"no active live session {session_id}")
        self.session_id = session_id


class LiveSession:
    """One takeover feeding the playback engine.

    The session owns a `StreamSource` and a pump task that drains it
    to the renderer. Two transports:

    - `start_webrtc()` — a phone-camera takeover: negotiate SDP on an
      RTCPeerConnection, wrap the inbound track in a
      `WebRtcStreamSource`, and arm a phantom-track watchdog.
    - `start_stream()` — a stream-transport takeover: wrap an
      `FfmpegStreamSource` (ffmpeg pulling the stream URL); no peer
      connection, no SDP, no watchdog.

    Either way the source yields RGB888 frames cover-fit-scaled to
    the renderer's native dimensions and the pump hands each to
    `renderer.render_frame` — same wire format the playlist source
    uses (§7.6). close() tears down whichever transport is in use.
    """

    # Phantom-session safety-net timeouts (Phase 12.1 Finding #2 +
    # 2026-05-24 security audit D3 Option C).
    #
    # Two distinct constants because the two transport paths have
    # different failure-detection budgets:
    #
    # WebRTC path: two failure detectors run in parallel.
    # 1. Primary: RTCPeerConnection.connectionState callback (wired
    #    in start_webrtc). Fires on aiortc state transitions; treats
    #    `failed` and `disconnected` as terminal and closes the
    #    session immediately. Catches mid-stream drops, ICE failures,
    #    DTLS failures -- typically within 1-2s of the actual network
    #    event. This is the audit-driven primary detector.
    # 2. Safety-net: _PHANTOM_TIMEOUT_SECONDS_WEBRTC (30s). With the
    #    state-change handler in place, this is for the genuinely-
    #    stuck-mid-negotiation case where aiortc never fires a state
    #    change (the handler catches most failures much faster).
    #
    # Stream-transport path: NO peer connection, so the only failure
    # detector is `_watch_for_first_frame` against
    # `_PHANTOM_TIMEOUT_SECONDS` (10s, unchanged from pre-audit).
    # Bumping this here would silently triple the stream phantom
    # window for ffmpeg-source failures, which is out of scope for
    # the audit (D3 was WebRTC-only).
    _PHANTOM_TIMEOUT_SECONDS = 10.0
    _PHANTOM_TIMEOUT_SECONDS_WEBRTC = 30.0

    def __init__(self, playback: PlaybackLoop):
        self._playback = playback
        self.id: UUID = uuid4()
        # Wall-clock timestamp at session creation. The Live UI's
        # Elapsed metric ticks against (now - started_at) read off
        # /api/live/status, so the value survives a panel re-mount
        # mid-session (Phase A.2 — closes the loop on QA's A.2 callout).
        # UTC explicit so the JSON response is unambiguous; the phone
        # subtracts wall-clock-now in the same UTC frame.
        self.started_at: datetime = datetime.now(UTC)
        # The frame source + pump are created by whichever start_*
        # transport method runs.
        self._source: WebRtcStreamSource | FfmpegStreamSource | None = None
        self._pump_task: asyncio.Task | None = None
        self._closed = False
        # WebRTC-transport-only state (stays None for a stream-transport session).
        self._pc: RTCPeerConnection | None = None
        # Phase 12.1 Finding #2: signaled when the first on_track
        # event fires. The phantom-session watchdog awaits this with
        # a timeout; if it never resolves, the session was never
        # backed by real media and the watchdog auto-closes.
        self._first_track_event = asyncio.Event()
        self._watchdog_task: asyncio.Task | None = None
        # Stream-transport-only state (stays unused for a WebRTC
        # session). Stream hardening C2 (findings M1+M2): signaled when
        # _pump renders its first frame. The stream first-frame
        # watchdog awaits this with a timeout; if it never resolves,
        # the stream URL was dead/unreachable and the watchdog auto-
        # closes — symmetric to the WebRTC phantom-track watchdog.
        self._first_frame_event = asyncio.Event()
        # Operator-driven pause: when True the pump drains the source
        # but does NOT call render_frame, so DRM scanout holds the
        # last painted frame on glass while the upstream source (VLC
        # publisher, phone camera) keeps producing. Resume = clear the
        # flag; the pump picks up at the *current* live frame (we do
        # not buffer + replay paused frames — operator expects "now",
        # not a delayed replay). Idempotent under repeat pause/resume.
        self._paused = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def paused(self) -> bool:
        return self._paused

    async def pause(self) -> None:
        """Freeze the on-glass frame; keep the upstream source alive.

        Sets the pump-side gate so subsequent frames are drained but
        not rendered. DRM scanout retains the last painted frame —
        no clear-to-black, no flicker. Idempotent: pausing an already-
        paused session is a no-op. Raises nothing; the LiveManager
        guards the session-id lookup."""
        self._paused = True

    async def resume(self) -> None:
        """Un-freeze: subsequent pump frames render as normal.

        Idempotent: resuming a non-paused session is a no-op.
        Operator picks up at the current live frame; we deliberately
        do not buffer + replay paused frames (latency over a multi-
        minute pause would be unbounded)."""
        self._paused = False

    async def start_webrtc(self, sdp_offer: str) -> str:
        """WebRTC takeover: set the remote offer, create an answer,
        pause playback.

        Returns the SDP answer (with ICE candidates baked in — non-trickle
        for v1 per §5.11). The caller hands this back to the phone in the
        same /api/live/start response.
        """
        # LEVER 2 lazy-imports (2026-06-24): pull the lazy module
        # attrs in as locals. The module-attribute access triggers
        # __getattr__ (PEP 562) on first call, lazy-loading
        # aiortc + stream_source (~10-20 MB RSS) only on the first
        # WebRTC takeover. Tests patch `openmarquee.live.RTC...`
        # directly in the module dict; that mock wins because the
        # dict is checked before __getattr__.
        import openmarquee.live as _self
        RTCPeerConnection = _self.RTCPeerConnection
        RTCSessionDescription = _self.RTCSessionDescription
        WebRtcStreamSource = _self.WebRtcStreamSource

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

        # 2026-05-24 security audit D3 Option C: primary failure
        # detector. aiortc fires connectionstatechange when the
        # underlying ICE / DTLS / SCTP transport state moves. Treat
        # `failed` and `disconnected` as terminal and tear down
        # immediately rather than waiting for the 30s safety-net.
        # Catches mid-stream network drops (phone walks out of WiFi
        # range, NAT rebinding fails) within ~1-2s of the actual
        # event vs the 10s the prior wall-clock-only design needed.
        #
        # close() is idempotent (guarded by self._closed) so a race
        # between this callback + the safety-net timeout firing for
        # the same session is harmless.
        @self._pc.on("connectionstatechange")
        def on_connection_state_change():
            # Defensive try/except: aiortc's pyee event emitter swallows
            # callback exceptions and the scheduled close() task would
            # surface as "Task exception was never retrieved" in the
            # asyncio log without being actionable. Wrap so any
            # unexpected error here is logged with context.
            try:
                state = self._pc.connectionState if self._pc is not None else "unknown"
                log.info("live: session %s connectionState -> %s", self.id, state)
                if state in ("failed", "disconnected") and not self._closed:
                    log.warning(
                        "live: session %s connectionState=%s; closing",
                        self.id,
                        state,
                    )
                    # Sync callback -> schedule async close via create_task.
                    asyncio.create_task(self.close())
            except Exception:
                log.exception(
                    "live: session %s connectionstatechange callback raised",
                    self.id,
                )

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
        # Closing flips _closed=True, which makes LiveManager.
        # is_active return False on the next /status query — the
        # phone will see the session has gone away.
        self._watchdog_task = asyncio.create_task(self._watch_for_first_track())
        # setLocalDescription may rewrite the SDP with gathered ICE
        # candidates; read the canonical form back from the PC.
        return self._pc.localDescription.sdp

    async def start_stream(self, stream_url: str) -> None:
        """Stream-transport takeover: pause playback and start pumping
        the stream source.

        There is no SDP and no peer connection — but there IS a first-
        frame watchdog, symmetric to the WebRTC phantom-track watchdog.
        The pump starts immediately (unlike WebRTC, there is no track
        to wait for); the watchdog catches the case where the pump
        never produces a frame (a dead/unreachable URL where
        ffprobe/ffmpeg hang or yield nothing) and auto-closes the
        session so the playlist resumes instead of freezing forever.
        The pump also auto-closes the session on ANY exit (ffmpeg
        crash, mid-stream disconnect, stream EOF) — see `_pump`.

        Security: `stream_url` is operator-supplied. FfmpegStreamSource
        constructs a StreamConsumer, whose `__init__` allowlist-
        validates the URL scheme and raises `ValueError` for a
        non-stream scheme (file://, pipe:, …) before any subprocess
        is spawned. That `ValueError` propagates out of start_stream →
        LiveManager.start/takeover → the /api/live route's
        catch-all, which returns a 400 — so the operator-takeover
        path is covered without a separate check here.
        """
        # LEVER 2 lazy-imports (2026-06-24): resolve FfmpegStreamSource
        # via module attribute access — see the __getattr__ at the
        # bottom of the file. Tests can patch
        # openmarquee.live.FfmpegStreamSource and the mock wins.
        import openmarquee.live as _self
        FfmpegStreamSource = _self.FfmpegStreamSource

        self._source = FfmpegStreamSource(self._playback.renderer, stream_url)
        await self._playback.pause()
        self._pump_task = asyncio.create_task(self._pump())
        # Stream hardening C2 (findings M1+M2): arm a first-frame
        # watchdog. If _pump never renders a frame within
        # _PHANTOM_TIMEOUT_SECONDS (dead URL, ffprobe/ffmpeg hangs),
        # auto-close so the playlist resumes. Symmetric to the WebRTC
        # phantom-track watchdog armed in start_webrtc().
        self._watchdog_task = asyncio.create_task(self._watch_for_first_frame())

    async def _watch_for_first_track(self) -> None:
        """Phase 12.1 Finding #2: auto-close if no track materializes
        within _PHANTOM_TIMEOUT_SECONDS_WEBRTC (30s safety-net; the
        connectionstatechange handler in start_webrtc catches most
        failures in 1-2s). Cancellation-safe: close() cancels this
        task, which surfaces as CancelledError here and is silently
        re-raised so the cancel completes."""
        try:
            await asyncio.wait_for(
                self._first_track_event.wait(),
                timeout=self._PHANTOM_TIMEOUT_SECONDS_WEBRTC,
            )
        except TimeoutError:
            if self._closed:
                # Session was closed via the normal path during the
                # wait — nothing to do.
                return
            log.warning(
                "live: session %s saw no track within %.1fs; closing as phantom",
                self.id,
                self._PHANTOM_TIMEOUT_SECONDS_WEBRTC,
            )
            await self.close()
        except asyncio.CancelledError:
            raise

    async def _watch_for_first_frame(self) -> None:
        """Stream hardening C2 (findings M1+M2): auto-close if _pump
        never renders a frame within _PHANTOM_TIMEOUT_SECONDS.

        Mirrors `_watch_for_first_track` (the WebRTC phantom-track
        watchdog) — same timeout constant, same TimeoutError → close()
        teardown. Catches a stream takeover to a dead/unreachable URL
        where ffprobe/ffmpeg hang or yield nothing: without this the
        session stays is_active and the playlist freezes on the last
        frame forever.

        This bounds the ZERO-frames-ever case; a pump that DID produce
        frames and then exited mid-stream is handled by `_pump`'s own
        finally-block close. Cancellation-safe: close() cancels this
        task, which surfaces as CancelledError here and is re-raised."""
        try:
            await asyncio.wait_for(
                self._first_frame_event.wait(),
                timeout=self._PHANTOM_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            if self._closed:
                # Session was closed via the normal path (or by the
                # pump's own teardown) during the wait — nothing to do.
                return
            log.warning(
                "live: session %s rendered no frame within %.1fs; closing as unreachable",
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
        a measured fps regression on the production sign.

        Stream hardening C2 (findings M1+M2): the pump self-closes the
        session on EVERY non-cancellation exit. The `finally` block
        below checks `_closed`: if the pump is exiting because the
        source ran dry (ffmpeg crashed, the stream disconnected, EOF)
        and the session is NOT already closing, it calls `close()` so
        the playlist RESUMES instead of freezing on the last frame
        forever. A close()-driven cancellation (the operator stopped
        the stream, or a takeover) already has `_closed=True`, so the
        `finally` is a no-op there — `close()` does not re-enter.
        The first rendered frame also signals `_first_frame_event`,
        which disarms the first-frame watchdog (`_watch_for_first_
        frame`) for a healthy takeover.
        """
        renderer = self._playback.renderer
        source = self._source
        assert source is not None  # _pump is only spawned with a source
        # HW-decode (2026-05-20): the source declares its pixel format.
        # An NV12 source (FfmpegStreamSource) yields source-resolution
        # frames; the renderer needs the source dims for its GPU cover-
        # fit. An RGB888 source (WebRtcStreamSource) needs neither —
        # render_frame()'s rgb888 default uses the panel dims.
        pixel_format = getattr(source, "pixel_format", "rgb888")
        try:
            async for frame_bytes in source.frames():
                # Disarm the first-frame watchdog as PROOF-OF-LIFE the
                # moment the source yields ANY frame — even one we
                # immediately discard for an operator pause. Otherwise
                # the watchdog would fire 10s into a pause-immediately-
                # after-start sequence and tear down a healthy session.
                # set() is idempotent so doing this every frame is
                # cheap and keeps the pump branch-free. (Moved above
                # the pause gate 2026-05-24 per subagent review.)
                self._first_frame_event.set()
                # Operator pause gate: drain the frame so the upstream
                # source (ffmpeg / WebRTC track) doesn't backpressure,
                # but skip the render call so DRM scanout holds the
                # last painted frame on glass. Resume = the flag flips
                # and the next iteration renders as normal.
                if self._paused:
                    continue
                frame_w = frame_h = None
                if pixel_format == "nv12":
                    # frame_dims() is known once the consumer's ffprobe
                    # has run — which is before its first frame, so this
                    # is populated by the time we get here.
                    dims = source.frame_dims() if hasattr(source, "frame_dims") else None
                    if dims is None:
                        log.error(
                            "live: NV12 source yielded a frame before "
                            "its source dimensions were known; stopping "
                            "the frame pump."
                        )
                        return
                    frame_w, frame_h = dims
                try:
                    # Perf-night r6 (2026-05-26): mirrors r5
                    # (44b013e _play_stream_slide). render_frame goes
                    # through _send_op's blocking subprocess.stdout.
                    # readline on the lazy-begin (rust_renderer.py:843-
                    # 891 — now resolved per r4-r6 sweep); every
                    # subsequent frame writes the binary frame
                    # channel without a readline, but the
                    # lazy-begin first-frame call IS the wedge. Wrap
                    # in asyncio.to_thread so the live-takeover pump
                    # — running at the source frame rate (~30fps for
                    # WebRTC, source fps for ffmpeg) — doesn't block
                    # the asyncio event loop for the IPC round-trip.
                    # Surfaced by the r6 regression-guard test
                    # (tests/test_async_sync_audit.py), missed by the
                    # initial human audit pass.
                    await asyncio.to_thread(
                        renderer.render_frame,
                        frame_bytes,
                        pixel_format=pixel_format,
                        frame_w=frame_w,
                        frame_h=frame_h,
                    )
                except Exception:
                    log.error(
                        "live: renderer rejected a pushed frame — "
                        "push-frame rendering is unavailable; stopping "
                        "the frame pump."
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
                # Perf-night r6: end_external_frames writes the
                # 0-length sentinel to the binary frame pipe + flush
                # (rust_renderer.py:709-733). Pipe-write wedge surface
                # is narrow but the asyncio.to_thread wrap is
                # consistent with r5's takedown wrap at playback.py:
                # 1307 and keeps the loop responsive during the
                # takedown round-trip on every pump exit path.
                await asyncio.to_thread(renderer.end_external_frames)
            except Exception:
                log.exception("live: end_external_frames failed")
            # Stream hardening C2 (findings M1+M2): a stream pump that
            # has exited MUST NOT leave the session is_active. If the
            # pump is here because the source ran dry (ffmpeg crashed,
            # the stream disconnected, EOF, an unreachable URL that
            # never yielded a frame) and the session is NOT already
            # closing, auto-close it so the playlist RESUMES instead of
            # freezing on the last frame forever.
            #
            # Gated to the stream transport (`_pc is None`): the WebRTC
            # transport deliberately does NOT tear down on track-end —
            # WebRtcStreamSource documents that contract (the pre-
            # refactor `_consume_video` behaviour), and the WebRTC path
            # has its own phantom-track watchdog for the no-media case.
            # C2's scope is the ffmpeg stream takeover only.
            #
            # The `_closed` guard skips this when close() drove the
            # exit (operator stop / takeover / the first-frame
            # watchdog): close() sets `_closed=True` before cancelling
            # the pump, so its own teardown does not re-enter. close()'s
            # `current_task()` guard makes the self-call from here safe
            # (no pump cancel/await of itself).
            if not self._closed and self._pc is None:
                await self.close()

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
        # detected via `asyncio.current_task()`. (Stream-transport
        # sessions also have a watchdog — _watch_for_first_frame —
        # which goes through this same cancel path; the None case
        # only applies if start_* raised before arming the watchdog.)
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
        # Skip the pump cancel/await when the pump is itself the caller
        # — stream hardening C2 routes a pump that exited on its own
        # (ffmpeg crash, disconnect, EOF) through close() from inside
        # the pump's `finally`. Cancelling + awaiting your own task
        # would self-deadlock; the pump is already on its way out, so
        # there is nothing to cancel. Self-call is detected via
        # `asyncio.current_task()`, the same way the watchdog cancel
        # above guards its self-cancel.
        if (
            self._pump_task is not None
            and not self._pump_task.done()
            and asyncio.current_task() is not self._pump_task
        ):
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


class LiveManager:
    """Single-publisher state. Owns at most one LiveSession at a time.

    Process-singleton in production (wired in dependencies.py); tests
    instantiate one per fixture.
    """

    def __init__(self, playback: PlaybackLoop):
        self._playback = playback
        self._session: LiveSession | None = None
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

        Surfaced through /api/live/status so the publishing phone's
        Elapsed counter ticks against the device's authoritative start
        time — survives a panel re-mount and is correct even if the
        phone's clock is skewed from the device's. None when no
        session is active.
        """
        return self._session.started_at if self.is_active else None

    @property
    def is_active_and_paused(self) -> bool:
        """True when there's an active session AND it's currently
        paused. Surfaced through /api/live/status.paused so the UI can
        reconcile a Pause/Resume button's state across panel
        re-mounts and reload. False when nothing is active."""
        return self.is_active and self._session is not None and self._session.paused

    @staticmethod
    async def _start_session(session: LiveSession, request: LiveStartBody) -> str | None:
        """Run the transport-specific start for `request`'s kind.

        Returns the SDP answer for a WebRTC start, or None for a
        stream-transport start (there is no answer to hand back)."""
        if isinstance(request, WebRtcStartRequest):
            return await session.start_webrtc(request.sdp_offer)
        return await session.start_stream(request.url)

    async def start(self, request: LiveStartBody) -> tuple[UUID, str | None]:
        """Negotiate a new session. Returns (session_id, sdp_answer);
        sdp_answer is None for a stream-transport start.

        Raises LiveAlreadyActive if a session is already running —
        the phone should switch to the take-over affordance.
        """
        async with self._lock:
            if self.is_active:
                assert self._session is not None
                raise LiveAlreadyActive(self._session.id)
            session = LiveSession(self._playback)
            try:
                answer_sdp = await self._start_session(session, request)
            except Exception:
                # Start failed — make sure we don't leak a half-open
                # transport and that playback resumes if pause fired.
                await session.close()
                raise
            self._session = session
            return (session.id, answer_sdp)

    async def takeover(self, request: LiveStartBody) -> tuple[UUID, str | None]:
        """Force-stop any active session, then start a new one.

        No-op-but-still-creates if nothing was active — the user got to
        Take Over slightly after the prior session ended naturally.
        """
        async with self._lock:
            if self._session is not None:
                await self._stop_locked()
            session = LiveSession(self._playback)
            try:
                answer_sdp = await self._start_session(session, request)
            except Exception:
                await session.close()
                raise
            self._session = session
            return (session.id, answer_sdp)

    async def stop(self, session_id: UUID) -> None:
        """Stop the named session. Raises LiveNotActive if it isn't
        the currently-active session (or nothing is active)."""
        async with self._lock:
            if self._session is None or self._session.id != session_id:
                raise LiveNotActive(session_id)
            await self._stop_locked()

    async def pause(self, session_id: UUID) -> None:
        """Pause the named session — DRM scanout holds the last
        rendered frame; the upstream source stays alive (so resume
        picks up at the current live frame, not a delayed buffer).
        Raises LiveNotActive if `session_id` doesn't match the
        currently-active session. Idempotent."""
        # No lock needed: we don't mutate self._session, only the
        # session's own flag. start/stop/takeover hold the lock for
        # session-creation atomicity; pause/resume don't touch that.
        if self._session is None or self._session.id != session_id:
            raise LiveNotActive(session_id)
        await self._session.pause()

    async def resume(self, session_id: UUID) -> None:
        """Resume the named session — pump renders the next frame
        from the upstream source. Idempotent: resuming a non-paused
        session is a no-op. Raises LiveNotActive on session-id
        mismatch — symmetric to pause()."""
        if self._session is None or self._session.id != session_id:
            raise LiveNotActive(session_id)
        await self._session.resume()

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


# LEVER 2 lazy-imports (2026-06-24): PEP 562 module-level __getattr__.
#
# Lazy-resolves the heavy WebRTC / stream symbols on FIRST module-
# attribute access. Caches the resolved value in `globals()` so
# subsequent accesses go straight to the module dict (no second
# __getattr__ call). Tests that `patch("openmarquee.live.RTC...")`
# write into the module dict directly, which Python checks BEFORE
# falling through to __getattr__ — so a patched mock wins over the
# lazy resolver, and the original on patch exit is restored.
#
# Import cost (per the dispatch):
#   - aiortc + aioice + pylibsrtp + av/ffmpeg ≈ ~10-15 MB RSS
#   - Pillow (via stream_source) ≈ ~5-8 MB RSS
#
# Both shifts to the first /api/live/start call instead of every
# backend boot. Prod devices that never go live save the entire cost.
_LAZY_NAMES = {
    "RTCPeerConnection",
    "RTCSessionDescription",
    "WebRtcStreamSource",
    "FfmpegStreamSource",
}


def __getattr__(name: str):
    if name not in _LAZY_NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if name in ("RTCPeerConnection", "RTCSessionDescription"):
        import aiortc

        value = getattr(aiortc, name)
    else:
        import openmarquee.stream_source as _stream_source

        value = getattr(_stream_source, name)
    globals()[name] = value
    return value
