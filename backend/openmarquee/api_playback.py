"""REST API for the playback engine.

POST /api/playback/start — kick off the loop (no-op if already running)
POST /api/playback/stop  — stop and wait for the loop to exit
GET  /api/playback/state — { is_running, current_item_id, current_item_type,
                            current_item_transition, current_item_transition_ms,
                            current_item_auto_mode, current_item_auto_format,
                            current_playlist_id }

The loop drives the device's renderer (MockRenderer in dev, HUB75/HDMI/etc.
on the device once those land). Replaced the Phase 2 manual
/dev/play/{id} poke (the dev endpoint has since been removed).
"""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from openmarquee.api import cors_headers_for_origin
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    get_content_storage,
    get_flock_storage,
    get_playback_loop,
    get_playlist_storage,
)
from openmarquee.flock import FlockStorage
from openmarquee.playback import PlaybackLoop
from openmarquee.playlist import PlaylistStorage

router = APIRouter(prefix="/api/playback", tags=["playback"])

LoopDep = Annotated[PlaybackLoop, Depends(get_playback_loop)]
ContentDep = Annotated[ContentStorage, Depends(get_content_storage)]
PlaylistDep = Annotated[PlaylistStorage, Depends(get_playlist_storage)]
FlockDep = Annotated[FlockStorage, Depends(get_flock_storage)]


class PlaybackState(BaseModel):
    is_running: bool
    current_item_id: UUID | None
    # "text_slide" | "image" | "video" | None — drives the live-preview
    # UI's choice of <video> vs <img>. Follows the ContentItem type
    # discriminator so adding a new content type auto-surfaces here.
    current_item_type: str | None
    # Transition that the *currently-displaying* PlaylistItem is
    # configured to perform on its way out ("cut" or "fade"). The live
    # preview keeps the last-seen pair in memory and replays a CSS
    # cross-fade when current_item_id changes, so the browser mirrors
    # what the device's playback loop does frame-by-frame.
    current_item_transition: str | None
    current_item_transition_ms: int | None
    # Auto-mode metadata for TextSlide items — drives the live preview's
    # client-side ticking overlay for time/date/day slides.
    current_item_auto_mode: str | None
    current_item_auto_format: str | None
    current_playlist_id: UUID | None


@router.get("/state", response_model=PlaybackState)
async def get_state(loop: LoopDep) -> PlaybackState:
    return PlaybackState(
        is_running=loop.is_running,
        current_item_id=loop.current_item_id,
        current_item_type=loop.current_item_type,
        current_item_transition=loop.current_item_transition,
        current_item_transition_ms=loop.current_item_transition_ms,
        current_item_auto_mode=loop.current_item_auto_mode,
        current_item_auto_format=loop.current_item_auto_format,
        current_playlist_id=loop.current_playlist_id,
    )


@router.get(
    "/current-thumbnail",
    response_class=FileResponse,
    responses={
        200: {"content": {"image/png": {}}},
        204: {"description": "Nothing is currently playing."},
        404: {"description": "Playlist's first item has no asset on disk."},
    },
)
async def get_current_thumbnail(
    request: Request,
    loop: LoopDep,
    content: ContentDep,
    playlists: PlaylistDep,
    flock_storage: FlockDep,
) -> Response:
    """Cover art for the playlist this device is currently playing —
    the first item of that playlist's asset, matching the playlist
    browser's convention (one stable visual per playlist).

    Drives the Flock panel's tile thumbnails. Using the current
    PLAYLIST's first slide (not the currently-displaying slide) keeps
    the tile visually stable as long as the same playlist is on — it
    only flips when the playlist itself changes (schedule rule kicks
    in, operator switches). Returns 204 when nothing is playing or
    the current playlist is empty.
    """
    if loop.current_item_id is None:
        # Nothing on screen → idle.
        return Response(status_code=204)

    playlist_id = loop.current_playlist_id
    first_id: UUID | None = None
    if playlist_id:
        playlist = playlists.get_by_id(playlist_id)
        if playlist is not None:
            ids = playlist.item_ids
            if ids:
                first_id = ids[0]
    # Fall back to the slide on screen so the tile never goes blank
    # during a race between a rename/delete and the next playback tick.
    if first_id is None:
        first_id = loop.current_item_id

    path = content.asset_path(first_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no asset for {first_id}")
    # Batch 11.3 / sweep #5 #4: CORS allowlist-reflective. Echo back the
    # Origin only when it's in the operator's flock allowlist (or a
    # builtin local origin). Same-origin requests don't carry Origin so
    # they fall through cleanly without any ACAO header (browser doesn't
    # need one).
    headers = {"Cache-Control": "no-store"}
    headers.update(cors_headers_for_origin(request.headers.get("origin", ""), flock_storage))
    return FileResponse(
        path,
        media_type="image/png",
        headers=headers,
    )


@router.get(
    "/current-frame",
    responses={
        200: {"content": {"image/png": {}}},
        503: {
            "description": "Capture not available (nothing playing or non-text/non-image slide)."
        },
    },
)
async def get_current_frame(
    request: Request,
    loop: LoopDep,
    flock_storage: FlockDep,
) -> Response:
    """PNG of what's actually rendering right now -- the slide's live
    composite at current elapsed_s, NOT the playlist cover art that
    /current-thumbnail returns. Distinct from /current-thumbnail in
    that it reflects motion + auto-mode (clock ticks, ticker
    position, etc.) at the captured moment.

    Backed by an in-memory cache in the playback loop with a 5-minute
    TTL plus immediate playlist-change invalidation. The cache caps
    capture cost at ~one renderer-capture IPC roundtrip per 5 minutes
    per distinct playlist; concurrent callers serialize behind a single
    lock so a burst of requests issues at most one capture.

    Returns 503 when nothing is playing OR the current slide type
    has no readback path (video). On error we honestly fail rather
    than fall back to the playlist cover -- the whole point of this
    endpoint is "real frame, or nothing." (Use /current-thumbnail if
    cover art is what the caller actually wants.)
    """
    png = await loop.cached_current_frame_png()
    if png is None:
        return Response(
            status_code=503,
            content="capture not available",
            media_type="text/plain",
        )
    # Batch 11.3 / sweep #5 #4: same allowlist-reflective ACAO as
    # /current-thumbnail above. See cors_headers_for_origin in api.py.
    headers = {"Cache-Control": "no-store"}
    headers.update(cors_headers_for_origin(request.headers.get("origin", ""), flock_storage))
    return Response(
        content=png,
        media_type="image/png",
        headers=headers,
    )


@router.post("/start", status_code=204)
async def start_playback(loop: LoopDep) -> None:
    await loop.start()


@router.post("/stop", status_code=204)
async def stop_playback(loop: LoopDep) -> None:
    await loop.stop()


# ---------------------------------------------------------------------
# Runtime perf-profile capture (perf-night r1, 2026-05-26).
#
# Foundation for perf measurement on a live Pi: enable phase-histogram
# collection over an N-frame window without restarting the sidecar.
# Restart would otherwise interrupt the operator's view (blank flash +
# reel-from-start) since the renderer runs as --ipc-sidecar.
#
# Auth: standard bearer gate (AuthMiddleware whitelist does NOT carve
# these out). Operator must POST with `Authorization: Bearer <token>`.
# ---------------------------------------------------------------------


class PerfStartRequest(BaseModel):
    """Frames to capture. 1..100000 — upper bound prevents an operator
    typo from pinning the sample store unbounded (each phase ~12 bytes
    per sample; 100k frames * ~10 phases ~12MB, well under any
    reasonable limit)."""

    frames: int = Field(ge=1, le=100_000)


class PerfDumpResponse(BaseModel):
    """Dump body. `ready=True` when frames_remaining=0 (capture window
    finished); `False` while still collecting OR profile disabled. The
    operator polls until ready=True, then reads `text` for the
    histogram. Distinguishing ready from "no samples" requires
    inspecting `text` — both states are useful to surface to the
    operator without invoking semantic interpretation here."""

    ready: bool
    text: str


@router.post("/perf/start", status_code=204)
async def perf_start(body: PerfStartRequest, loop: LoopDep) -> None:
    """Enable runtime phase profiling for the next `frames` frames.
    Replaces --profile-frames CLI flag with no sidecar restart.

    Returns 503 when the active renderer doesn't support profile
    capture (MockRenderer in dev/test). Production RustRenderer always
    supports it.
    """
    renderer = loop.renderer
    profile_start = getattr(renderer, "profile_start", None)
    if profile_start is None:
        raise HTTPException(
            status_code=503,
            detail="active renderer does not support profile capture (only RustRenderer does)",
        )
    # _send_op is blocking + acquires the IPC RLock; wrap in to_thread
    # so we don't wedge the event loop while the playback advance() may
    # be holding the lock. See rust_renderer.py:861 TODO -- this IS the
    # FastAPI request-handler context that TODO calls out.
    await asyncio.to_thread(profile_start, body.frames)


@router.get("/perf/dump", response_model=PerfDumpResponse)
async def perf_dump(loop: LoopDep) -> PerfDumpResponse:
    """Poll the accumulated histogram. `ready=True` when
    `frames_remaining=0` appears in the body's first line.

    Returns 503 when the active renderer doesn't support profile
    capture (MockRenderer in dev/test).
    """
    renderer = loop.renderer
    profile_dump = getattr(renderer, "profile_dump", None)
    if profile_dump is None:
        raise HTTPException(
            status_code=503,
            detail="active renderer does not support profile capture (only RustRenderer does)",
        )
    text = await asyncio.to_thread(profile_dump)
    ready = "frames_remaining=0" in text
    return PerfDumpResponse(ready=ready, text=text)
