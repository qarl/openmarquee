"""REST API for playlists — UUID-keyed.

Collection endpoints — manage any playlist by id:
    GET    /api/playlists           — { schema_version, playlists: [...] }
    POST   /api/playlists           — create a new playlist (server assigns id)
    GET    /api/playlists/{id}      — single playlist
    PUT    /api/playlists/{id}      — replace name + items (id immutable)
    DELETE /api/playlists/{id}      — remove
"""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from openmarquee.dependencies import get_playlist_storage
from openmarquee.playlist import (
    Playlist,
    PlaylistCollection,
    PlaylistItem,
    PlaylistStorage,
)

router = APIRouter(tags=["playlist"])

PlaylistDep = Annotated[PlaylistStorage, Depends(get_playlist_storage)]


class PlaylistUpdate(BaseModel):
    """Wire format for PUT requests on either endpoint.

    `name` is the editable display label. `items` is the canonical entry
    list; `item_ids` is the legacy shape (each id becomes a PlaylistItem
    with default transitions).
    """

    name: str | None = None
    items: list[PlaylistItem] | None = None
    item_ids: list[UUID] | None = None

    def to_items(self) -> list[PlaylistItem]:
        if self.items is not None:
            return self.items
        if self.item_ids is not None:
            return [PlaylistItem(item_id=i) for i in self.item_ids]
        return []


class PlaylistCreate(BaseModel):
    """Wire format for POST /api/playlists."""

    name: str = Field(default="", max_length=200)
    items: list[PlaylistItem] | None = None
    item_ids: list[UUID] | None = None

    def to_items(self) -> list[PlaylistItem]:
        if self.items is not None:
            return self.items
        if self.item_ids is not None:
            return [PlaylistItem(item_id=i) for i in self.item_ids]
        return []


@router.get("/api/playlists", response_model=PlaylistCollection)
async def list_playlists(storage: PlaylistDep) -> PlaylistCollection:
    return storage.load_all()


@router.post("/api/playlists", response_model=Playlist, status_code=201)
async def create_playlist(
    payload: PlaylistCreate, storage: PlaylistDep
) -> Playlist:
    playlist = Playlist(
        id=uuid4(),
        name=payload.name,
        items=payload.to_items(),
    )
    storage.set_by_id(playlist)
    return playlist


@router.get("/api/playlists/{playlist_id}", response_model=Playlist)
async def get_playlist_by_id(
    playlist_id: UUID, storage: PlaylistDep
) -> Playlist:
    playlist = storage.get_by_id(playlist_id)
    if playlist is None:
        raise HTTPException(status_code=404, detail=f"no playlist with id {playlist_id}")
    return playlist


@router.put("/api/playlists/{playlist_id}", response_model=Playlist)
async def set_playlist_by_id(
    playlist_id: UUID, payload: PlaylistUpdate, storage: PlaylistDep
) -> Playlist:
    existing = storage.get_by_id(playlist_id)
    # Preserve the existing name if the payload doesn't override it.
    name = (
        payload.name
        if payload.name is not None
        else (existing.name if existing else "")
    )
    playlist = Playlist(id=playlist_id, name=name, items=payload.to_items())
    storage.set_by_id(playlist)
    return playlist


@router.delete("/api/playlists/{playlist_id}", status_code=204)
async def delete_playlist(playlist_id: UUID, storage: PlaylistDep) -> None:
    # The default playlist is load-bearing — content uploads auto-append
    # to it. Allow deletion so users can clear it; the next upload will
    # recreate an empty one with the same DEFAULT_PLAYLIST_ID.
    if not storage.delete_by_id(playlist_id):
        raise HTTPException(status_code=404, detail=f"no playlist with id {playlist_id}")
