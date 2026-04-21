"""REST API for the persistent playlist.

GET /api/playlist        — { item_ids: [uuid, ...] }
PUT /api/playlist        — replace the playlist with the given order
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from openmarquee.dependencies import get_playlist_storage
from openmarquee.playlist import Playlist, PlaylistStorage

router = APIRouter(prefix="/api/playlist", tags=["playlist"])

PlaylistDep = Annotated[PlaylistStorage, Depends(get_playlist_storage)]


class PlaylistUpdate(BaseModel):
    """Wire format for PUT /api/playlist.

    Structurally identical to `Playlist` today; kept separate so the wire
    schema can evolve (e.g. adding ETag/version for optimistic concurrency)
    without touching the domain model.
    """

    item_ids: list[UUID] = Field(default_factory=list)


@router.get("", response_model=Playlist)
async def get_playlist(storage: PlaylistDep) -> Playlist:
    return storage.load()


@router.put("", response_model=Playlist)
async def set_playlist(payload: PlaylistUpdate, storage: PlaylistDep) -> Playlist:
    playlist = Playlist(item_ids=payload.item_ids)
    storage.save(playlist)
    return playlist
