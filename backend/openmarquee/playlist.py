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
from uuid import UUID

from pydantic import BaseModel, Field


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
