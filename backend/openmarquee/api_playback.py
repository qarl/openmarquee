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

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    get_content_storage,
    get_playback_loop,
    get_playlist_storage,
)
from openmarquee.playback import PlaybackLoop
from openmarquee.playlist import PlaylistStorage

router = APIRouter(prefix="/api/playback", tags=["playback"])

LoopDep = Annotated[PlaybackLoop, Depends(get_playback_loop)]
ContentDep = Annotated[ContentStorage, Depends(get_content_storage)]
PlaylistDep = Annotated[PlaylistStorage, Depends(get_playlist_storage)]


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
    loop: LoopDep, content: ContentDep, playlists: PlaylistDep
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
    # No-store so the browser's <img> with ?t=<ms> cachebust works; CORS
    # wildcard so one device's UI can pull this from a peer's backend.
    return FileResponse(
        path,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.post("/start", status_code=204)
async def start_playback(loop: LoopDep) -> None:
    await loop.start()


@router.post("/stop", status_code=204)
async def stop_playback(loop: LoopDep) -> None:
    await loop.stop()
