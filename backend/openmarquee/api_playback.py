"""REST API for the playback engine.

POST /api/playback/start — kick off the loop (no-op if already running)
POST /api/playback/stop  — stop and wait for the loop to exit
GET  /api/playback/state — { is_running, current_item_id, current_item_type,
                            current_item_transition, current_item_transition_ms,
                            current_item_auto_mode, current_item_auto_format,
                            current_playlist_name }

The loop drives the device's renderer (MockRenderer in dev, HUB75/HDMI/etc.
on the device once those land). This obsoletes the manual /dev/play/{id}
poke for the normal flow — that endpoint stays around for one-off tests.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from openmarquee.dependencies import get_playback_loop
from openmarquee.playback import PlaybackLoop

router = APIRouter(prefix="/api/playback", tags=["playback"])

LoopDep = Annotated[PlaybackLoop, Depends(get_playback_loop)]


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
    current_playlist_name: str | None


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
        current_playlist_name=loop.current_playlist_name,
    )


@router.post("/start", status_code=204)
async def start_playback(loop: LoopDep) -> None:
    await loop.start()


@router.post("/stop", status_code=204)
async def stop_playback(loop: LoopDep) -> None:
    await loop.stop()
