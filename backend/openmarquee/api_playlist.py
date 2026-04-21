"""REST API for persistent named playlists.

Singular (legacy) endpoints — operate on the default playlist:
    GET /api/playlist          — { item_ids: [uuid, ...] }
    PUT /api/playlist          — replace the default playlist's order

Plural (multi-playlist) endpoints — manage any named playlist:
    GET    /api/playlists           — { name: { item_ids: [...] }, ... }
    GET    /api/playlists/{name}    — single playlist
    PUT    /api/playlists/{name}    — create or replace
    DELETE /api/playlists/{name}    — remove (no-op + 404 if absent)
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from openmarquee.dependencies import get_playlist_storage
from openmarquee.playlist import (
    DEFAULT_PLAYLIST_NAME,
    Playlist,
    PlaylistCollection,
    PlaylistItem,
    PlaylistStorage,
)

router = APIRouter(tags=["playlist"])

PlaylistDep = Annotated[PlaylistStorage, Depends(get_playlist_storage)]


class PlaylistUpdate(BaseModel):
    """Wire format for PUT requests on either endpoint.

    Accepts two shapes:
      - `{items: [{item_id, transition, transition_ms}, ...]}` — canonical.
      - `{item_ids: [uuid, ...]}` — legacy. Each id becomes a PlaylistItem
        with default transitions (cut / 500ms). The UI's reorder path on
        older bundles still works through this shoulder.
    """

    items: list[PlaylistItem] | None = None
    item_ids: list[UUID] | None = None

    def to_playlist(self) -> Playlist:
        if self.items is not None:
            return Playlist(items=self.items)
        if self.item_ids is not None:
            return Playlist(items=[PlaylistItem(item_id=i) for i in self.item_ids])
        return Playlist()


# --- legacy single-playlist endpoints ---


@router.get("/api/playlist", response_model=Playlist)
async def get_default_playlist(storage: PlaylistDep) -> Playlist:
    return storage.load()


@router.put("/api/playlist", response_model=Playlist)
async def set_default_playlist(payload: PlaylistUpdate, storage: PlaylistDep) -> Playlist:
    playlist = payload.to_playlist()
    storage.save(playlist)
    return playlist


# --- multi-playlist endpoints ---


@router.get("/api/playlists", response_model=PlaylistCollection)
async def list_playlists(storage: PlaylistDep) -> PlaylistCollection:
    return storage.load_all()


@router.get("/api/playlists/{name}", response_model=Playlist)
async def get_playlist_by_name(name: str, storage: PlaylistDep) -> Playlist:
    return storage.get_playlist(name)


@router.put("/api/playlists/{name}", response_model=Playlist)
async def set_playlist_by_name(
    name: str, payload: PlaylistUpdate, storage: PlaylistDep
) -> Playlist:
    playlist = payload.to_playlist()
    storage.set_playlist(name, playlist)
    return playlist


@router.delete("/api/playlists/{name}", status_code=204)
async def delete_playlist(name: str, storage: PlaylistDep) -> None:
    if name == DEFAULT_PLAYLIST_NAME:
        # The default playlist is load-bearing — content uploads auto-append
        # to it. Allow deletion so users can clear it, but warn via status:
        # actually, just allow it and let the next upload recreate. No 4xx.
        pass
    if not storage.delete_playlist(name):
        raise HTTPException(status_code=404, detail=f"no playlist named {name!r}")
