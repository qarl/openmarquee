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

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from openmarquee.dependencies import get_stream_manager
from openmarquee.stream import StreamAlreadyActive, StreamManager, StreamNotActive

router = APIRouter(prefix="/api/stream", tags=["stream"])

StreamDep = Annotated[StreamManager, Depends(get_stream_manager)]


class StreamStartRequest(BaseModel):
    """Phone-published SDP offer. v1 only carries video — `audio: false`
    at getUserMedia (§5.11), so the offer's audio m-section is rejected
    by the answer side and never decoded."""

    sdp_offer: str


class StreamStartResponse(BaseModel):
    """SDP answer + session id. Phone applies the answer to its
    RTCPeerConnection and the WebRTC handshake completes."""

    session_id: UUID
    sdp_answer: str


class StreamStopRequest(BaseModel):
    session_id: UUID


class HardwareTier(BaseModel):
    """What the phone should clamp getUserMedia constraints to.

    Today this is a static report keyed off the §5.11 basic-tier row
    (Pi Zero 2 W, 480p/30). Phase 12.3 live-fire replaces the constant
    with real hardware detection — if the tier needs to drop to 360p,
    that change is local to this struct.
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
    """

    state: Literal["idle", "active"]
    session_id: UUID | None
    tier: HardwareTier


# §5.11 basic tier. Phase 12.3 hardware live-fire validates the
# 480p/30 number; if SW VP8/H.264 decode in aiortc can't sustain it on
# Pi Zero 2 W, the tier drops to 360p/30 and only this constant changes.
_BASIC_TIER = HardwareTier(name="basic", max_width=854, max_height=480, max_fps=30)


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
        raise HTTPException(
            status_code=400,
            detail=f"stream negotiation failed: {exc}",
        ) from exc
    return StreamStartResponse(session_id=session_id, sdp_answer=answer)


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
        raise HTTPException(
            status_code=400,
            detail=f"stream takeover failed: {exc}",
        ) from exc
    return StreamStartResponse(session_id=session_id, sdp_answer=answer)


@router.get("/status", response_model=StreamStatus)
async def stream_status(streams: StreamDep) -> StreamStatus:
    return StreamStatus(
        state="active" if streams.is_active else "idle",
        session_id=streams.active_session_id,
        tier=_BASIC_TIER,
    )
