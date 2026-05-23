"""REST API for live takeover (SYSTEM_SPEC §5.11 + §6 +
docs/STREAM_VLC_PROPOSAL.md).

A takeover preempts the playlist with a live source. The request body
is a `kind`-tagged union (LiveStartBody):

- kind="webrtc" — a phone publishes a WebRTC video track; the backend
  negotiates one SDP round trip (offer in, answer out).
- kind="stream" — the operator publishes a network stream URL; the Pi
  pulls it with ffmpeg. No SDP, so the response's sdp_answer is null.

Either way LiveManager pauses the playback loop and pushes decoded
frames to the renderer. Endpoints:

POST /api/live/start    — start a new session (409 if one's active)
POST /api/live/stop     — tear down a session by id (404 if gone)
GET  /api/live/status   — idle | active + active session_id + tier
POST /api/live/takeover — force-stop the active session and start fresh

Non-trickle ICE for the WebRTC path: all candidates are baked into the
SDP answer, so /start does the full SDP round trip in a single
response. The phone hands the answer to its RTCPeerConnection.
"""

import logging
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from openmarquee.dependencies import get_live_manager
from openmarquee.live import (
    LiveAlreadyActive,
    LiveManager,
    LiveNotActive,
    LiveStartBody,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/live", tags=["live"])

LiveDep = Annotated[LiveManager, Depends(get_live_manager)]


class LiveStartResponse(BaseModel):
    """Session id (+ SDP answer for a WebRTC start). The phone applies
    the answer to its RTCPeerConnection to complete the handshake; an
    RTSP start has no answer, so `sdp_answer` is null there.

    `started_at` is the wall-clock UTC timestamp the device assigned
    when the session was created — the phone's Elapsed counter ticks
    against (now - started_at) so it's correct even if the phone's
    clock is skewed from the device's, and survives a panel re-mount
    mid-session (Phase A.2)."""

    session_id: UUID
    sdp_answer: str | None = None
    started_at: datetime


class LiveStopRequest(BaseModel):
    session_id: UUID


class HardwareTier(BaseModel):
    """The capture / decode caps a live source should clamp to.

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


class LiveStatus(BaseModel):
    """Polled by the phone before every Go Live tap.

    `state == "active"` with a session_id different from the caller's
    means another phone owns the screen — the phone shows the
    "Take over" affordance instead of "Go Live".

    `started_at` is the wall-clock UTC timestamp of the active
    session's creation, or None when idle. Lets a panel that mounts
    mid-session pick up the Elapsed counter from /status without
    needing to have observed the original /start response (Phase A.2).
    """

    state: Literal["idle", "active"]
    session_id: UUID | None
    started_at: datetime | None
    tier: HardwareTier


# Live-takeover hardware tiers (SYSTEM_SPEC §5.11 + STREAM_VLC_PROPOSAL §7).
# Phase 12.3 hardware live-fire (STREAM/VLC slice 9) validates these
# numbers and adds real per-device detection; until then /status
# reports a static tier. If SW H.264 decode can't sustain a number on
# the target Pi, only these constants change.
_BASIC_TIER = HardwareTier(name="basic", max_width=854, max_height=480, max_fps=30)
_GOOD_TIER = HardwareTier(name="good", max_width=1920, max_height=1080, max_fps=30)

# Per-source tier table. Both the phone-camera (webrtc) and the
# network-stream sources run at the basic tier today; lifting the
# single constant into this table lets later profiling give the two
# sources distinct caps without touching call sites.
_SOURCE_TIERS: dict[str, HardwareTier] = {
    "webrtc": _BASIC_TIER,
    "stream": _BASIC_TIER,
}


@router.post("/start", response_model=LiveStartResponse)
async def start_live(
    payload: LiveStartBody,
    live: LiveDep,
) -> LiveStartResponse:
    try:
        session_id, answer = await live.start(payload)
    except LiveAlreadyActive as exc:
        # 409 carries the active session id so the phone can offer
        # "Take over" without a second round trip to /status.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "live_already_active",
                "active_session_id": str(exc.active_session_id),
            },
        ) from exc
    except Exception as exc:
        # SDP parse failure / aiortc raised. 400 since the phone's
        # request is the most likely source of badness.
        # 11.2: don't reflect the exception string into the response --
        # aiortc/SDP-parse messages can carry internals. Log + opaque 400.
        log.exception("live negotiation failed")
        raise HTTPException(
            status_code=400,
            detail="live negotiation failed",
        ) from exc
    started_at = live.active_session_started_at
    assert started_at is not None  # session was just created above
    return LiveStartResponse(
        session_id=session_id, sdp_answer=answer, started_at=started_at
    )


@router.post("/stop", status_code=204)
async def stop_live(
    payload: LiveStopRequest,
    live: LiveDep,
) -> None:
    try:
        await live.stop(payload.session_id)
    except LiveNotActive as exc:
        raise HTTPException(
            status_code=404,
            detail=f"no active session {exc.session_id}",
        ) from exc


@router.post("/takeover", response_model=LiveStartResponse)
async def takeover_live(
    payload: LiveStartBody,
    live: LiveDep,
) -> LiveStartResponse:
    """Force-stop whatever's active and start a new session in one
    request. Phone hits this when the user ack'd the "someone else
    is streaming" warning and tapped Take Over."""
    try:
        session_id, answer = await live.takeover(payload)
    except Exception as exc:
        # 11.2: don't reflect the exception string. Log + opaque 400.
        log.exception("live takeover failed")
        raise HTTPException(
            status_code=400,
            detail="live takeover failed",
        ) from exc
    started_at = live.active_session_started_at
    assert started_at is not None  # session was just created above
    return LiveStartResponse(
        session_id=session_id, sdp_answer=answer, started_at=started_at
    )


@router.get("/status", response_model=LiveStatus)
async def live_status(live: LiveDep) -> LiveStatus:
    return LiveStatus(
        state="active" if live.is_active else "idle",
        session_id=live.active_session_id,
        started_at=live.active_session_started_at,
        # /status is polled by the phone before Go Live, so it reports
        # the webrtc (phone-camera) source's tier.
        tier=_SOURCE_TIERS["webrtc"],
    )
