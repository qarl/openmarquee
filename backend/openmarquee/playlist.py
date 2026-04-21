"""Persistent playlist — an ordered list of content item IDs.

Per SYSTEM_SPEC §3.3, the playlist is a single JSON file on the SD card,
not a database. The `Playlist` model is just the ordering; the actual
content lives in `ContentStorage` and is referenced by id.

Items in the playlist that no longer exist in storage are tolerated —
the playback loop's fetch callable (wired in (a2)) will skip ids it
can't load, so a stale id can't crash the loop. Cleanup happens lazily
on the next PUT /api/playlist (the UI sees what's actually playable
and overwrites).
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from openmarquee.content import ContentItem
    from openmarquee.content.storage import ContentStorage


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


class PlaylistStorage:
    """Persists the playlist as a single JSON file with atomic writes."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Playlist:
        """Return the persisted playlist, or an empty one if no file exists yet."""
        if not self.path.exists():
            return Playlist()
        data = json.loads(self.path.read_text())
        return Playlist.model_validate(data)

    def save(self, playlist: Playlist) -> None:
        """Atomically write the playlist to disk.

        Same write-to-tmp + rename pattern as ContentStorage so a crashed
        process can't leave a half-written playlist that load() would choke on.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(playlist.model_dump_json(indent=2))
        tmp.replace(self.path)


def list_in_playlist_order(
    content_storage: "ContentStorage",
    playlist_storage: PlaylistStorage,
) -> list["ContentItem"]:
    """Return all stored content items, ordered by the persisted playlist.

    Items present in the playlist appear first, in playlist order. Items in
    storage but missing from the playlist (orphans — uploaded before this
    feature, or not yet appended) are appended at the end sorted by id.
    Items in the playlist that no longer exist in storage are silently
    skipped.

    This is the single canonical "what items, in what order" function for
    both the saved-slides list view and the playback engine.
    """
    items_by_id = {item.id: item for item in content_storage.list_all()}
    playlist = playlist_storage.load()

    ordered: list[ContentItem] = []
    used: set[UUID] = set()
    for item_id in playlist.item_ids:
        if item_id in items_by_id and item_id not in used:
            ordered.append(items_by_id[item_id])
            used.add(item_id)

    # Append orphans deterministically (sorted by id string).
    orphans = [item for item_id, item in items_by_id.items() if item_id not in used]
    orphans.sort(key=lambda item: str(item.id))
    ordered.extend(orphans)
    return ordered
