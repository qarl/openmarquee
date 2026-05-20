"""REST API for live stream takeover (SYSTEM_SPEC §5.11 + §6).

Phone publishes a WebRTC video track; the backend's StreamManager
negotiates one round-trip (offer in, answer out), pauses the playback
loop, and starts pushing decoded frames to the renderer. Endpoints:

POST /api/stream/start    — start a new session (409 if one's active)
POST /api/stream/stop     — tear down a session by id (404 if gone)
GET  /api/stream/status   — idle | active + active session_id + tier
POST /api/stream/takeover — force-stop the active session and start fresh

Non-trickle ICE for v1: all candidates are baked into the SDP answer,
so /start does the full SDP round trip in a single response. The phone
hands the answer to its RTCPeerConnection and frames flow.
"""

import logging
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from openmarquee.dependencies import get_stream_manager
from openmarquee.stream import StreamAlreadyActive, StreamManager, StreamNotActive

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["stream"])

StreamDep = Annotated[StreamManager, Depends(get_stream_manager)]


class StreamStartRequest(BaseModel):
    """Phone-published SDP offer. v1 only carries video — `audio: false`
    at getUserMedia (§5.11), so the offer's audio m-section is rejected
    by the answer side and never decoded."""

    sdp_offer: str


class StreamStartResponse(BaseModel):
    """SDP answer + session id. Phone applies the answer to its
    RTCPeerConnection and the WebRTC handshake completes.

    `started_at` is the wall-clock UTC timestamp the device assigned
    when the session was created — the phone's Elapsed counter ticks
    against (now - started_at) so it's correct even if the phone's
    clock is skewed from the device's, and survives a panel re-mount
    mid-stream (Phase A.2)."""

    session_id: UUID
    sdp_answer: str
    started_at: datetime


class StreamStopRequest(BaseModel):
    session_id: UUID


class HardwareTier(BaseModel):
    """The capture / decode caps a stream source should clamp to.

    `basic` = Pi Zero 2 W (854×480/30), `good` = Pi 4/5
    (1920×1080/30); `future` is reserved for the Phase 12.3 hardware
    live-fire row. Today /status reports a static tier; STREAM/VLC
    slice 9 live-fire adds real per-device detection — if a tier
    number needs to change, that change stays local to this module.
    """

    name: Literal["basic", "good", "future"]
    max_width: int
    max_height: int
    max_fps: int


class StreamStatus(BaseModel):
    """Polled by the phone before every Go Live tap.

    `state == "active"` with a session_id different from the caller's
    means another phone owns the screen — the phone shows the
    "Take over" affordance instead of "Go Live".

    `started_at` is the wall-clock UTC timestamp of the active
    session's creation, or None when idle. Lets a panel that mounts
    mid-stream pick up the Elapsed counter from /status without
    needing to have observed the original /start response (Phase A.2).
    """

    state: Literal["idle", "active"]
    session_id: UUID | None
    started_at: datetime | None
    tier: HardwareTier


# Stream hardware tiers (SYSTEM_SPEC §5.11 + STREAM_VLC_PROPOSAL §7).
# Phase 12.3 hardware live-fire (STREAM/VLC slice 9) validates these
# numbers and adds real per-device detection; until then /status
# reports a static tier. If SW H.264 decode can't sustain a number on
# the target Pi, only these constants change.
_BASIC_TIER = HardwareTier(name="basic", max_width=854, max_height=480, max_fps=30)
_GOOD_TIER = HardwareTier(name="good", max_width=1920, max_height=1080, max_fps=30)

# Per-source tier table. Both the phone-camera (webrtc) and the VLC
# (rtsp) sources run at the basic tier today; lifting the single
# constant into this table lets later profiling give the two sources
# distinct caps without touching call sites.
_SOURCE_TIERS: dict[str, HardwareTier] = {
    "webrtc": _BASIC_TIER,
    "rtsp": _BASIC_TIER,
}


@router.post("/start", response_model=StreamStartResponse)
async def start_stream(
    payload: StreamStartRequest,
    streams: StreamDep,
) -> StreamStartResponse:
    try:
        session_id, answer = await streams.start(payload.sdp_offer)
    except StreamAlreadyActive as exc:
        # 409 carries the active session id so the phone can offer
        # "Take over" without a second round trip to /status.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "stream_already_active",
                "active_session_id": str(exc.active_session_id),
            },
        ) from exc
    except Exception as exc:
        # SDP parse failure / aiortc raised. 400 since the phone's
        # request is the most likely source of badness.
        # 11.2: don't reflect the exception string into the response --
        # aiortc/SDP-parse messages can carry internals. Log + opaque 400.
        log.exception("stream negotiation failed")
        raise HTTPException(
            status_code=400,
            detail="stream negotiation failed",
        ) from exc
    started_at = streams.active_session_started_at
    assert started_at is not None  # session was just created above
    return StreamStartResponse(
        session_id=session_id, sdp_answer=answer, started_at=started_at
    )


@router.post("/stop", status_code=204)
async def stop_stream(
    payload: StreamStopRequest,
    streams: StreamDep,
) -> None:
    try:
        await streams.stop(payload.session_id)
    except StreamNotActive as exc:
        raise HTTPException(
            status_code=404,
            detail=f"no active session {exc.session_id}",
        ) from exc


@router.post("/takeover", response_model=StreamStartResponse)
async def takeover_stream(
    payload: StreamStartRequest,
    streams: StreamDep,
) -> StreamStartResponse:
    """Force-stop whatever's active and start a new session in one
    request. Phone hits this when the user ack'd the "someone else
    is streaming" warning and tapped Take Over."""
    try:
        session_id, answer = await streams.takeover(payload.sdp_offer)
    except Exception as exc:
        # 11.2: don't reflect the exception string. Log + opaque 400.
        log.exception("stream takeover failed")
        raise HTTPException(
            status_code=400,
            detail="stream takeover failed",
        ) from exc
    started_at = streams.active_session_started_at
    assert started_at is not None  # session was just created above
    return StreamStartResponse(
        session_id=session_id, sdp_answer=answer, started_at=started_at
    )


@router.get("/status", response_model=StreamStatus)
async def stream_status(streams: StreamDep) -> StreamStatus:
    return StreamStatus(
        state="active" if streams.is_active else "idle",
        session_id=streams.active_session_id,
        started_at=streams.active_session_started_at,
        # /status is polled by the phone before Go Live, so it reports
        # the webrtc (phone-camera) source's tier.
        tier=_SOURCE_TIERS["webrtc"],
    )
