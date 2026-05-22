"""REST API for playlists — UUID-keyed plus a default-playlist shorthand.

Collection endpoints — manage any playlist by id:
    GET    /api/playlists           — { schema_version, playlists: [...] }
    POST   /api/playlists           — create a new playlist (server assigns id)
    GET    /api/playlists/{id}      — single playlist
    PUT    /api/playlists/{id}      — replace name + items (id immutable)
    DELETE /api/playlists/{id}      — remove

Default-playlist shorthand (SYSTEM_SPEC §6) — alias for DEFAULT_PLAYLIST_ID:
    GET    /api/playlist            — get the default playlist
    PUT    /api/playlist            — update the default playlist
"""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from openmarquee.dependencies import get_playlist_storage
from openmarquee.playlist import (
    DEFAULT_PLAYLIST_ID,
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
async def create_playlist(payload: PlaylistCreate, storage: PlaylistDep) -> Playlist:
    playlist = Playlist(
        id=uuid4(),
        name=payload.name,
        items=payload.to_items(),
    )
    storage.set_by_id(playlist)
    return playlist


@router.get("/api/playlist", response_model=Playlist)
async def get_default_playlist(storage: PlaylistDep) -> Playlist:
    """Default-playlist GET shorthand — SYSTEM_SPEC §6.

    Delegates to the UUID-explicit handler with DEFAULT_PLAYLIST_ID so
    response semantics (body shape, 404 behavior) stay byte-identical
    to the explicit form. The default playlist is bootstrapped at
    storage load, so on a fresh device this returns the seeded empty
    playlist, not a 404 — but the 404 path remains live for the
    operator-deletes-then-immediately-GETs window before the next
    content upload re-creates it.
    """
    return await get_playlist_by_id(DEFAULT_PLAYLIST_ID, storage)


@router.put("/api/playlist", response_model=Playlist)
async def set_default_playlist(payload: PlaylistUpdate, storage: PlaylistDep) -> Playlist:
    """Default-playlist PUT shorthand — SYSTEM_SPEC §6.

    Delegates to the UUID-explicit PUT handler with DEFAULT_PLAYLIST_ID,
    so the name-preservation-when-omitted + items-replacement semantics
    match the explicit form exactly.
    """
    return await set_playlist_by_id(DEFAULT_PLAYLIST_ID, payload, storage)


@router.get("/api/playlists/{playlist_id}", response_model=Playlist)
async def get_playlist_by_id(playlist_id: UUID, storage: PlaylistDep) -> Playlist:
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
    name = payload.name if payload.name is not None else (existing.name if existing else "")
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
