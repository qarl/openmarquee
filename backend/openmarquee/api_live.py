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

# Low-security-DiD Bundle C item 6 (2026-05-25): on the wire we only
# surface exception CLASS NAMES that are public-stdlib types
# operators can self-diagnose against (a netlink OSError, a parse-
# bad-int ValueError, etc.). Anything else -- crucially aiortc
# internals like SdpParseError -- maps to "internal_error" so an
# authed prober can't fingerprint the library version to chase CVEs.
# The full traceback still lands in the backend log via the
# log.exception calls below; operator-self-diagnosis through the
# log path is unaffected.
_DIAGNOSABLE_EXCEPTION_CLASSES = frozenset(
    {
        "OSError",  # netlink EAFNOSUPPORT (FYS 2026-05-23), socket errors
        "ValueError",  # malformed-int / malformed-enum past Pydantic
        "TimeoutError",  # ICE gather timeout, asyncio wait-for
        "ConnectionError",  # peer-connection failures mid-negotiation
    }
)


def _safe_error_class(exc: BaseException) -> str:
    """Map an exception to a wire-safe class-name string. Whitelisted
    public-stdlib names pass through; everything else (including
    aiortc internals + custom backend exception classes) collapses
    to "internal_error" to avoid library-identity leakage."""
    name = type(exc).__name__
    return name if name in _DIAGNOSABLE_EXCEPTION_CLASSES else "internal_error"


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

    `paused` is True when the operator has paused the session — DRM
    scanout is holding the last rendered frame while the upstream
    source stays alive. Always False when state == "idle".
    """

    state: Literal["idle", "active"]
    session_id: UUID | None
    started_at: datetime | None
    tier: HardwareTier
    paused: bool = False


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
        #
        # 11.2: never reflect the exception MESSAGE into the response
        # (aiortc/SDP-parse messages can carry paths + internals). The
        # full traceback goes to the backend log via log.exception
        # below — that's the operator's diagnosis path.
        #
        # On the wire we surface only the exception CLASS NAME, and
        # only if it's in the public-stdlib whitelist at
        # _DIAGNOSABLE_EXCEPTION_CLASSES (OSError / ValueError /
        # TimeoutError / ConnectionError). Anything else -- aiortc
        # internals like SdpParseError, custom backend classes --
        # collapses to "internal_error" via _safe_error_class so an
        # authed prober can't fingerprint the library version (Bundle
        # C item 6, 2026-05-25). The reason class names ship at all:
        # without a class hint, a remote diagnoser (operator on a
        # phone, harness in a browser) sees just "live_negotiation_
        # failed" and has to ssh into the device to see the actual
        # exception — that cost 25 min of hunting for an OSError
        # [Errno 97] (netlink-EAFNOSUPPORT, FYS 2026-05-23). The
        # whitelisted names preserve that operator-diagnosis value.
        log.exception("live negotiation failed")
        raise HTTPException(
            status_code=400,
            detail={
                "error": "live_negotiation_failed",
                "error_class": _safe_error_class(exc),
            },
        ) from exc
    started_at = live.active_session_started_at
    assert started_at is not None  # session was just created above
    return LiveStartResponse(session_id=session_id, sdp_answer=answer, started_at=started_at)


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
        # 11.2 + Slice 3 (2026-05-23): never reflect the exception
        # message; surface only the class name on the wire. Symmetric
        # with /api/live/start above — same rationale + safety profile.
        log.exception("live takeover failed")
        raise HTTPException(
            status_code=400,
            detail={
                "error": "live_takeover_failed",
                "error_class": _safe_error_class(exc),
            },
        ) from exc
    started_at = live.active_session_started_at
    assert started_at is not None  # session was just created above
    return LiveStartResponse(session_id=session_id, sdp_answer=answer, started_at=started_at)


@router.post("/pause", status_code=204)
async def pause_live(
    payload: LiveStopRequest,
    live: LiveDep,
) -> None:
    """Pause the active session — DRM scanout holds the last rendered
    frame; the upstream source stays alive. Idempotent. 404 if the
    session_id doesn't match the currently-active session."""
    try:
        await live.pause(payload.session_id)
    except LiveNotActive as exc:
        raise HTTPException(
            status_code=404,
            detail=f"no active session {exc.session_id}",
        ) from exc


@router.post("/resume", status_code=204)
async def resume_live(
    payload: LiveStopRequest,
    live: LiveDep,
) -> None:
    """Resume a paused session — pump renders the next live frame from
    the upstream source. Idempotent. 404 if the session_id doesn't
    match the currently-active session."""
    try:
        await live.resume(payload.session_id)
    except LiveNotActive as exc:
        raise HTTPException(
            status_code=404,
            detail=f"no active session {exc.session_id}",
        ) from exc


@router.get("/status", response_model=LiveStatus)
async def live_status(live: LiveDep) -> LiveStatus:
    return LiveStatus(
        state="active" if live.is_active else "idle",
        session_id=live.active_session_id,
        started_at=live.active_session_started_at,
        # /status is polled by the phone before Go Live, so it reports
        # the webrtc (phone-camera) source's tier.
        tier=_SOURCE_TIERS["webrtc"],
        paused=live.is_active_and_paused,
    )
