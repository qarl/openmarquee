"""REST API for the playback engine.

POST /api/playback/start — kick off the loop (no-op if already running)
POST /api/playback/stop  — stop and wait for the loop to exit
GET  /api/playback/state — { is_running, current_item_id }

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
    current_playlist_name: str | None


@router.get("/state", response_model=PlaybackState)
async def get_state(loop: LoopDep) -> PlaybackState:
    return PlaybackState(
        is_running=loop.is_running,
        current_item_id=loop.current_item_id,
        current_playlist_name=loop.current_playlist_name,
    )


@router.post("/start", status_code=204)
async def start_playback(loop: LoopDep) -> None:
    await loop.start()


@router.post("/stop", status_code=204)
async def stop_playback(loop: LoopDep) -> None:
    await loop.stop()
