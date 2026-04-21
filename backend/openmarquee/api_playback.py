"""REST API for the playback engine.

POST /api/playback/start — kick off the loop (no-op if already running)
POST /api/playback/stop  — stop and wait for the loop to exit
GET  /api/playback/state — { is_running, current_item_id, current_item_type,
                            current_item_pipeline, current_playlist_name }

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
    # For video items: "h264_mp4" | "raw_frames" | None. The preview
    # can't embed a raw_frames stream in a <video>, so it falls back
    # to the thumbnail when this field is "raw_frames".
    current_item_pipeline: str | None
    current_playlist_name: str | None


@router.get("/state", response_model=PlaybackState)
async def get_state(loop: LoopDep) -> PlaybackState:
    return PlaybackState(
        is_running=loop.is_running,
        current_item_id=loop.current_item_id,
        current_item_type=loop.current_item_type,
        current_item_pipeline=loop.current_item_pipeline,
        current_playlist_name=loop.current_playlist_name,
    )


@router.post("/start", status_code=204)
async def start_playback(loop: LoopDep) -> None:
    await loop.start()


@router.post("/stop", status_code=204)
async def stop_playback(loop: LoopDep) -> None:
    await loop.stop()
