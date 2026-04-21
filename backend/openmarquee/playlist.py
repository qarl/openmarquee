"""Persistent playlists — named ordered lists of content item IDs.

Per SYSTEM_SPEC §3.3, playlists live as JSON on the SD card, not in a
database. The data model is `dict[str, list[UUID]]` — many named playlists,
each just an ordered list of ids. Content storage holds the actual items;
playlists only carry order.

Items in a playlist that no longer exist in storage are tolerated — the
playback loop's fetch path skips ids it can't load, so a stale id can't
crash the loop. Cleanup happens lazily on the next PUT (the UI sees what's
actually playable and overwrites).

Default playlist:

`DEFAULT_PLAYLIST_NAME` ("default") is the playlist the content lifecycle
auto-appends to on upload and auto-removes from on delete. Other named
playlists can be created via the multi-playlist API but aren't fed
automatically — users curate them. The schedule evaluator selects which
playlist plays at any given time; missing / unknown playlist names are
treated as empty (the playback loop polls instead of erroring).

Storage format: a single JSON file at `path`, containing an envelope:

    {
        "schema_version": 2,
        "playlists": { "default": { "item_ids": [...] }, ... }
    }

Backwards compat: `schema_version == 1` (or no envelope at all) is the
single-playlist format that shipped through Phase 5 (a). Load() migrates
it transparently to a single "default" playlist on first read.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from openmarquee.content import ContentItem
    from openmarquee.content.storage import ContentStorage

PLAYLIST_SCHEMA_VERSION = 2
DEFAULT_PLAYLIST_NAME = "default"


class Playlist(BaseModel):
    """An ordered list of content item IDs."""

    item_ids: list[UUID] = Field(default_factory=list)

    def append(self, item_id: UUID) -> None:
        """Add an id to the end if it isn't already present."""
        if item_id not in self.item_ids:
            self.item_ids.append(item_id)

    def remove(self, item_id: UUID) -> None:
        """Remove an id if present; no-op otherwise."""
        if item_id in self.item_ids:
            self.item_ids.remove(item_id)


class PlaylistCollection(BaseModel):
    """All playlists, by name. The on-disk envelope."""

    schema_version: int = Field(default=PLAYLIST_SCHEMA_VERSION)
    playlists: dict[str, Playlist] = Field(default_factory=dict)


class PlaylistStorage:
    """Persists the named-playlist collection as a single JSON file with
    atomic writes.

    The legacy single-playlist API (`load`, `save`) still works against the
    `default` playlist for callers that haven't been refactored yet —
    typically the content auto-append/remove plumbing.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    # --- multi-playlist primitives ---

    def load_all(self) -> PlaylistCollection:
        """Return the full collection of named playlists."""
        if not self.path.exists():
            return PlaylistCollection()
        data = json.loads(self.path.read_text())
        return _coerce_to_collection(data)

    def save_all(self, collection: PlaylistCollection) -> None:
        """Atomically write the full collection to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(collection.model_dump_json(indent=2))
        tmp.replace(self.path)

    def get_playlist(self, name: str) -> Playlist:
        """Return the named playlist, or an empty Playlist if it doesn't exist.

        Tolerant lookup — the playback loop and the schedule evaluator can
        both reference any name without pre-checking.
        """
        return self.load_all().playlists.get(name, Playlist())

    def set_playlist(self, name: str, playlist: Playlist) -> None:
        """Create or replace a named playlist."""
        collection = self.load_all()
        collection.playlists[name] = playlist
        self.save_all(collection)

    def delete_playlist(self, name: str) -> bool:
        """Remove a named playlist. Returns True if it existed, False otherwise.

        The DEFAULT_PLAYLIST_NAME playlist can be deleted but will be
        recreated empty on the next content upload.
        """
        collection = self.load_all()
        if name not in collection.playlists:
            return False
        del collection.playlists[name]
        self.save_all(collection)
        return True

    def all_names(self) -> list[str]:
        """Return all playlist names sorted alphabetically."""
        return sorted(self.load_all().playlists)

    # --- legacy single-playlist API (operates on DEFAULT_PLAYLIST_NAME) ---

    def load(self) -> Playlist:
        """Legacy: return the default playlist, or an empty one."""
        return self.get_playlist(DEFAULT_PLAYLIST_NAME)

    def save(self, playlist: Playlist) -> None:
        """Legacy: replace the default playlist."""
        self.set_playlist(DEFAULT_PLAYLIST_NAME, playlist)


def _coerce_to_collection(data: dict) -> PlaylistCollection:
    """Accept either the new envelope or the legacy single-playlist format.

    Legacy: `{"item_ids": [...]}` → `{"playlists": {"default": {...}}}`.
    """
    if "item_ids" in data and "playlists" not in data:
        # Legacy v1 single-playlist format. Migrate to v2 collection.
        return PlaylistCollection(
            playlists={DEFAULT_PLAYLIST_NAME: Playlist.model_validate(data)},
        )
    return PlaylistCollection.model_validate(data)


def list_in_playlist_order(
    content_storage: "ContentStorage",
    playlist_storage: PlaylistStorage,
    playlist_name: str = DEFAULT_PLAYLIST_NAME,
) -> list["ContentItem"]:
    """Return content items ordered by the named playlist.

    Items present in the playlist appear first, in playlist order. Items in
    storage but missing from the playlist (orphans — uploaded before this
    feature, or not yet appended) are appended at the end sorted by id ONLY
    if the named playlist is the default. For non-default playlists, only
    the explicitly-included items appear; users curate those by hand.

    Items in the playlist that no longer exist in storage are silently
    skipped.

    This is the single canonical "what items, in what order" function for
    both the saved-slides list view (default playlist) and the playback
    engine (whichever playlist the schedule selected).
    """
    items_by_id = {item.id: item for item in content_storage.list_all()}
    collection = playlist_storage.load_all()
    target = collection.playlists.get(playlist_name, Playlist())

    ordered: list[ContentItem] = []
    used: set[UUID] = set()
    for item_id in target.item_ids:
        if item_id in items_by_id and item_id not in used:
            ordered.append(items_by_id[item_id])
            used.add(item_id)

    if playlist_name == DEFAULT_PLAYLIST_NAME:
        # Append true orphans — items in storage but referenced by NO
        # playlist. Items in named playlists belong to those, not to default.
        all_referenced: set[UUID] = set()
        for p in collection.playlists.values():
            all_referenced.update(p.item_ids)
        orphans = [
            item
            for item_id, item in items_by_id.items()
            if item_id not in used and item_id not in all_referenced
        ]
        orphans.sort(key=lambda item: str(item.id))
        ordered.extend(orphans)

    return ordered
