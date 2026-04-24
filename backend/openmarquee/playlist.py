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

Storage format (v3): a single JSON file at `path`, containing an envelope:

    {
        "schema_version": 3,
        "playlists": {
            "default": {
                "items": [
                    {"item_id": "...", "transition": "cut", "transition_ms": 500},
                    ...
                ]
            },
            ...
        }
    }

Backwards compat: `schema_version == 2` stored items as `item_ids: [uuid]`
— transitions lived on the slide model at that time. `schema_version == 1`
(or no envelope at all) was the single-playlist form that shipped through
Phase 5 (a). Both migrate transparently on load — each legacy id becomes
a PlaylistItem with default transitions.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, Field, computed_field, model_validator

if TYPE_CHECKING:
    from openmarquee.content import ContentItem
    from openmarquee.content.storage import ContentStorage

PLAYLIST_SCHEMA_VERSION = 3
DEFAULT_PLAYLIST_NAME = "default"


class PlaylistItem(BaseModel):
    """One entry in a playlist: the content id + the transition OUT of
    this slide into the NEXT one.

    Transitions live here (on the playlist) rather than on the slide so
    a single slide can play with different fades in different playlists,
    and so the same slide appearing twice in a row can have different
    follow-ons — both were impossible under the slide-side model.
    """

    item_id: UUID
    transition: Literal["cut", "fade", "wipe", "slide", "iris"] = "cut"
    transition_ms: int = Field(default=500, ge=0, le=5000)


class Playlist(BaseModel):
    """An ordered list of PlaylistItems."""

    items: list[PlaylistItem] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_item_ids(cls, data):
        """Back-compat shim: `Playlist(item_ids=[uuid, ...])` still works,
        wrapping each id in a PlaylistItem with defaults. Keeps both the
        call-site API and persisted-v2 JSON round-trippable through this
        constructor without every caller threading PlaylistItem through.
        """
        if isinstance(data, dict) and "item_ids" in data and "items" not in data:
            ids = data.pop("item_ids")
            data["items"] = [{"item_id": i} for i in ids]
        return data

    @computed_field
    @property
    def item_ids(self) -> list[UUID]:
        """Read-only list of the item ids in order. Serialized as a
        convenience field so v2-era clients (UI bundles that haven't
        migrated to reading `items`) keep working — they see the same
        shape they've always seen alongside the new `items` field."""
        return [i.item_id for i in self.items]

    def append(
        self,
        item_id: UUID,
        transition: Literal["cut", "fade", "wipe", "slide", "iris"] = "cut",
        transition_ms: int = 500,
    ) -> None:
        """Add an id to the end if it isn't already present. Default
        transitions match what the v2 → v3 migrator stamps on legacy
        entries."""
        if item_id not in self.item_ids:
            self.items.append(
                PlaylistItem(
                    item_id=item_id,
                    transition=transition,
                    transition_ms=transition_ms,
                )
            )

    def remove(self, item_id: UUID) -> None:
        """Remove an id if present; no-op otherwise."""
        self.items = [i for i in self.items if i.item_id != item_id]


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

    def prune_dangling_refs(self, valid_ids: set) -> int:
        """Drop any playlist items whose `item_id` isn't in `valid_ids`.

        Returns the number of entries removed. No-op if nothing is stale.
        Useful at lifespan startup to recover from a dev-style wipe of
        content/ that left the playlist JSON intact — the pallet /
        playback loop would otherwise serve dangling references.
        """
        collection = self.load_all()
        pruned_count = 0
        for name, playlist in collection.playlists.items():
            kept = [it for it in playlist.items if it.item_id in valid_ids]
            if len(kept) != len(playlist.items):
                pruned_count += len(playlist.items) - len(kept)
                collection.playlists[name] = Playlist(items=kept)
        if pruned_count:
            self.save_all(collection)
        return pruned_count

    # --- legacy single-playlist API (operates on DEFAULT_PLAYLIST_NAME) ---

    def load(self) -> Playlist:
        """Legacy: return the default playlist, or an empty one."""
        return self.get_playlist(DEFAULT_PLAYLIST_NAME)

    def save(self, playlist: Playlist) -> None:
        """Legacy: replace the default playlist."""
        self.set_playlist(DEFAULT_PLAYLIST_NAME, playlist)


def _coerce_to_collection(data: dict) -> PlaylistCollection:
    """Accept the v3 envelope or migrate from v2 / v1.

    - v1 (pre-Phase-5): `{"item_ids": [uuid, ...]}` — one unnamed
      playlist. Each id gets default transitions.
    - v2 (Phase 5 through 2026-04-21): `{"playlists": {"default":
      {"item_ids": [...]}, ...}}` — named collection, transitions
      lived on the slide model. Each id gets default transitions.
    - v3 (today): `{"playlists": {"default": {"items": [{"item_id":
      ..., "transition": ..., "transition_ms": ...}]}, ...}}`.

    Both legacy forms are migrated silently so existing SD cards keep
    working after an upgrade.
    """
    # Legacy v1: unnamed single playlist.
    if "item_ids" in data and "playlists" not in data:
        return PlaylistCollection(
            playlists={
                DEFAULT_PLAYLIST_NAME: _playlist_from_legacy_item_ids(
                    data.get("item_ids", [])
                )
            },
        )

    # v2 or v3: named collection. Each playlist may be v2 (`item_ids`) or
    # v3 (`items`); promote v2 entries.
    playlists = data.get("playlists", {})
    migrated: dict[str, Playlist] = {}
    for name, raw in playlists.items():
        if isinstance(raw, dict) and "item_ids" in raw and "items" not in raw:
            migrated[name] = _playlist_from_legacy_item_ids(raw["item_ids"])
        else:
            migrated[name] = Playlist.model_validate(raw)
    return PlaylistCollection(
        schema_version=data.get("schema_version", PLAYLIST_SCHEMA_VERSION),
        playlists=migrated,
    )


def _playlist_from_legacy_item_ids(item_ids: list) -> Playlist:
    """v1/v2 → v3 lift: wrap each raw id in a PlaylistItem with defaults."""
    return Playlist(items=[PlaylistItem(item_id=UUID(str(i))) for i in item_ids])


def list_in_playlist_order(
    content_storage: "ContentStorage",
    playlist_storage: PlaylistStorage,
    playlist_name: str = DEFAULT_PLAYLIST_NAME,
    *,
    include_orphans: bool = False,
) -> list["ContentItem"]:
    """Return content items, playlist order first.

    Default (`include_orphans=False`) returns STRICTLY the items in
    the named playlist — what the playback loop iterates, so bundled
    library assets (seed backgrounds, demo videos) don't leak onto
    the sign unless explicitly added to a playlist.

    With `include_orphans=True`, items present in storage but not
    referenced by ANY playlist are appended at the end (sorted by id).
    That's what the UI pallets + the text-editor's background picker
    want — the library view shows everything the device has stored,
    not just what's currently scheduled to play.

    Items referenced by the playlist but missing from storage are
    silently skipped.
    """
    items_by_id = {item.id: item for item in content_storage.list_all()}
    collection = playlist_storage.load_all()
    target = collection.playlists.get(playlist_name, Playlist())

    ordered: list[ContentItem] = []
    used: set[UUID] = set()
    for p_item in target.items:
        if p_item.item_id in items_by_id and p_item.item_id not in used:
            # Patch the content item's transition fields with the playlist's
            # values. Since v3 the playlist owns transitions — the content
            # model's fields are legacy-only. model_copy returns a copy so
            # the same content reappearing in a different playlist can carry
            # different transitions.
            content = items_by_id[p_item.item_id]
            ordered.append(
                content.model_copy(
                    update={
                        "transition": p_item.transition,
                        "transition_ms": p_item.transition_ms,
                    }
                )
            )
            used.add(p_item.item_id)

    if include_orphans:
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
