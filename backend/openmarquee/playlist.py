"""Persistent playlists — UUID-keyed ordered lists of content item IDs.

Per SYSTEM_SPEC §3.3, playlists live as JSON on the SD card, not in a
database.

Identity: each playlist has a stable UUID `id` plus a human-editable
`name` (display label). Renaming a playlist NEVER changes its id —
schedule rules, the default-playlist pointer, and any other reference
key off `id`, so renames are safe.

Items in a playlist that no longer exist in storage are tolerated — the
playback loop's fetch path skips ids it can't load, so a stale id can't
crash the loop. Cleanup happens lazily on the next PUT (the UI sees what's
actually playable and overwrites).

Default playlist:

`DEFAULT_PLAYLIST_ID` is a constant UUID for the playlist the content
lifecycle auto-appends to on upload and auto-removes from on delete.
Other playlists can be created via the multi-playlist API but aren't fed
automatically — users curate them. The schedule evaluator selects which
playlist plays at any given time; missing / unknown playlist ids are
treated as empty (the playback loop polls instead of erroring).

Storage format (v4): a single JSON file at `path`, containing an envelope:

    {
        "schema_version": 4,
        "playlists": [
            {
                "id": "00000000-0000-4000-8000-000000000001",
                "name": "default",
                "items": [
                    {"item_id": "...", "transition": "cut", "transition_ms": 500},
                    ...
                ]
            },
            ...
        ]
    }

Backwards compat: `schema_version <= 3` stored playlists as
`dict[name, Playlist]`. Each named playlist gets a fresh UUID assigned
on load (the default playlist gets `DEFAULT_PLAYLIST_ID` so its
identity is stable across migrations). Schedule rules referring to
playlists by name will need to be migrated separately — see
`migrate_schedule_v1_to_v2` in schedule.py.

`schema_version <= 2` stored items as `item_ids: [uuid]` (transitions
on the slide model). Each id becomes a PlaylistItem with default
transitions. `schema_version == 1` (or no envelope at all) was the
single-playlist form — a single unnamed playlist's `item_ids` list.
All legacy forms migrate transparently on load.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

if TYPE_CHECKING:
    from openmarquee.content import ContentItem
    from openmarquee.content.storage import ContentStorage

PLAYLIST_SCHEMA_VERSION = 4

# Stable UUID for the default playlist. Constant across all devices so
# the v3→v4 migration produces the same id, and so callers (content
# upload, tests) can reference the default playlist without a name lookup.
# This is a UUID4 generated once and frozen here — DO NOT change it.
DEFAULT_PLAYLIST_ID = UUID("00000000-0000-4000-8000-000000000001")
# Display name applied to the default playlist on fresh-install bootstrap
# and on legacy-save-path coercion. Was "default" through Phase 5; renamed
# to "Welcome" 2026-04-28 to match the seeded slide content. The change
# is name-only — the playlist's identity is its UUID, so schedule rules
# / API references keep working across the rename.
DEFAULT_PLAYLIST_NAME = "Welcome"
# Legacy name used in v2/v3 storage to identify which playlist is the
# default during migration. Kept as "default" so devices upgrading from
# pre-v4 schemas still match their existing default playlist by name —
# without this anchor, the migration would treat the legacy "default"
# entry as just another named playlist and insert a new (empty) Welcome
# playlist alongside it.
LEGACY_DEFAULT_PLAYLIST_NAME = "default"


class PlaylistItem(BaseModel):
    """One entry in a playlist: the content id + the transition OUT of
    this slide into the NEXT one.

    Transitions live here (on the playlist) rather than on the slide so
    a single slide can play with different fades in different playlists,
    and so the same slide appearing twice in a row can have different
    follow-ons — both were impossible under the slide-side model.
    """

    item_id: UUID
    transition: Literal[
        "cut", "fade", "wipe", "slide", "iris", "scroll", "flip", "marquee", "dissolve", "pixelate", "halftone", "scanline", "glitch", "push", "blinds"
    ] = "cut"
    transition_ms: int = Field(default=500, ge=0, le=5000)


class Playlist(BaseModel):
    """A playlist with a stable UUID `id` and an editable display `name`.

    The `id` is the foreign key — schedule rules, the default-playlist
    pointer, and any other reference uses `id`, so renames don't break
    references. `name` is purely cosmetic.
    """

    # Forward-compat: tolerate unknown fields on load + round-trip them
    # through save_all. Lets a downgrade scenario (or hand-added operator
    # metadata) survive the auto-migration rewrite without silent loss.
    model_config = ConfigDict(extra="allow")

    id: UUID = Field(default_factory=uuid4)
    name: str = Field(default="", max_length=200)
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
        transition: Literal[
            "cut",
            "fade",
            "wipe",
            "slide",
            "iris",
            "scroll",
            "flip",
            "marquee",
            "dissolve",
            "pixelate",
            "halftone",
            "scanline",
            "glitch",
            "push",
            "blinds",
        ] = "cut",
        transition_ms: int = 500,
    ) -> None:
        """Add an id to the end if it isn't already present."""
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
    """All playlists, as an ordered list. The on-disk envelope."""

    model_config = ConfigDict(extra="allow")

    schema_version: int = Field(default=PLAYLIST_SCHEMA_VERSION)
    playlists: list[Playlist] = Field(default_factory=list)

    def by_id(self, playlist_id: UUID) -> Playlist | None:
        """Return the playlist with the given id, or None."""
        for p in self.playlists:
            if p.id == playlist_id:
                return p
        return None

    def by_name(self, name: str) -> Playlist | None:
        """Return the FIRST playlist with the given name, or None.

        Names are not unique — two playlists can share a display label.
        Use `by_id` for guaranteed-unique resolution. This helper exists
        for the v3→v4 schedule migration (which has only names to work
        from) and for tests that don't care which one wins.
        """
        for p in self.playlists:
            if p.name == name:
                return p
        return None


class PlaylistStorage:
    """Persists the playlist collection as a single JSON file with
    atomic writes.

    The legacy single-playlist API (`load`, `save`) still works against
    the default playlist (resolved by `DEFAULT_PLAYLIST_ID`) for callers
    that haven't been refactored yet — typically the content
    auto-append/remove plumbing.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    # --- collection primitives ---

    def load_all(self) -> PlaylistCollection:
        """Return the full collection. Migrates legacy formats on the fly
        (v1/v2/v3 → v4) AND rewrites the file on a successful migration
        so subsequent loads skip the conversion."""
        if not self.path.exists():
            return _bootstrap_default_collection()
        data = json.loads(self.path.read_text())
        collection, was_migrated = _coerce_to_collection(data)
        if was_migrated:
            self.save_all(collection)
        return collection

    def save_all(self, collection: PlaylistCollection) -> None:
        """Atomically write the full collection to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(collection.model_dump_json(indent=2))
        tmp.replace(self.path)

    def get_by_id(self, playlist_id: UUID) -> Playlist | None:
        """Return the playlist with the given id, or None."""
        return self.load_all().by_id(playlist_id)

    def set_by_id(self, playlist: Playlist) -> None:
        """Create or replace a playlist by id. The playlist's `id` field
        is the lookup key; `name` and `items` are the writable fields."""
        collection = self.load_all()
        existing = collection.by_id(playlist.id)
        if existing is not None:
            idx = collection.playlists.index(existing)
            collection.playlists[idx] = playlist
        else:
            collection.playlists.append(playlist)
        self.save_all(collection)

    def delete_by_id(self, playlist_id: UUID) -> bool:
        """Remove a playlist by id. Returns True if it existed.

        The default playlist can be deleted but will be recreated empty
        (with the same DEFAULT_PLAYLIST_ID) on the next content upload.
        """
        collection = self.load_all()
        target = collection.by_id(playlist_id)
        if target is None:
            return False
        collection.playlists.remove(target)
        self.save_all(collection)
        return True

    def all_ids(self) -> list[UUID]:
        """Return all playlist ids in collection order."""
        return [p.id for p in self.load_all().playlists]

    def prune_dangling_refs(self, valid_ids: set) -> int:
        """Drop any playlist items whose `item_id` isn't in `valid_ids`.

        Returns the number of entries removed. No-op if nothing is stale.
        Useful at lifespan startup to recover from a dev-style wipe of
        content/ that left the playlist JSON intact — the pallet /
        playback loop would otherwise serve dangling references.
        """
        collection = self.load_all()
        pruned_count = 0
        for playlist in collection.playlists:
            kept = [it for it in playlist.items if it.item_id in valid_ids]
            if len(kept) != len(playlist.items):
                pruned_count += len(playlist.items) - len(kept)
                playlist.items = kept
        if pruned_count:
            self.save_all(collection)
        return pruned_count

    # --- legacy single-playlist API (operates on DEFAULT_PLAYLIST_ID) ---

    def load(self) -> Playlist:
        """Legacy: return the default playlist, or an empty one."""
        existing = self.get_by_id(DEFAULT_PLAYLIST_ID)
        if existing is not None:
            return existing
        return Playlist(id=DEFAULT_PLAYLIST_ID, name=DEFAULT_PLAYLIST_NAME)

    def save(self, playlist: Playlist) -> None:
        """Legacy: replace the default playlist. Coerces the id to
        DEFAULT_PLAYLIST_ID and the name to DEFAULT_PLAYLIST_NAME
        ("Welcome") so the legacy single-playlist callers don't
        accidentally lose the default identity."""
        defaulted = playlist.model_copy(
            update={"id": DEFAULT_PLAYLIST_ID, "name": DEFAULT_PLAYLIST_NAME}
        )
        self.set_by_id(defaulted)


def _bootstrap_default_collection() -> PlaylistCollection:
    """Return a collection containing just an empty default playlist.

    Used when the storage file doesn't exist yet — gives the system a
    deterministic starting point with the default playlist's stable id.
    """
    return PlaylistCollection(
        playlists=[Playlist(id=DEFAULT_PLAYLIST_ID, name=DEFAULT_PLAYLIST_NAME)]
    )


def _coerce_to_collection(data: dict) -> tuple[PlaylistCollection, bool]:
    """Accept the v4 envelope or migrate from v3 / v2 / v1.

    Returns `(collection, was_migrated)` so callers can persist the
    upgraded form back to disk.

    - v1 (pre-Phase-5): `{"item_ids": [uuid, ...]}` — one unnamed
      playlist's items. Becomes the default playlist with default
      transitions on each id.
    - v2 (Phase 5): `{"playlists": {"default": {"item_ids": [...]},
      ...}}` — dict-keyed by name, transitions on slides. Each id gets
      default transitions; each name gets a fresh UUID (default name
      gets the constant DEFAULT_PLAYLIST_ID).
    - v3 (2026-04-21 to ~04-25): `{"playlists": {"default": {"items":
      [...]}, ...}}` — dict-keyed, transitions on items. UUIDs assigned
      same way as v2.
    - v4 (today): `{"playlists": [{"id": ..., "name": ..., "items":
      [...]}, ...]}` — list with explicit id + name on each playlist.
    """
    schema = data.get("schema_version", 1)

    # v4: already in target form.
    if schema >= PLAYLIST_SCHEMA_VERSION and "playlists" in data and isinstance(
        data["playlists"], list
    ):
        return PlaylistCollection.model_validate(data), False

    # v1: unnamed single playlist.
    if "item_ids" in data and "playlists" not in data:
        legacy = _playlist_items_from_legacy_item_ids(data.get("item_ids", []))
        return (
            PlaylistCollection(
                playlists=[
                    Playlist(
                        id=DEFAULT_PLAYLIST_ID,
                        name=DEFAULT_PLAYLIST_NAME,
                        items=legacy,
                    )
                ]
            ),
            True,
        )

    # v2 or v3: dict-keyed by name.
    raw_playlists = data.get("playlists", {})
    migrated: list[Playlist] = []
    # Only the FIRST playlist named "default" (the LEGACY name used
    # through v3) gets the constant id — preserves the existing default
    # playlist's identity across the rename to "Welcome". If a
    # hand-edited file or a restored backup somehow contains two
    # "default" entries, the second gets a fresh UUID rather than
    # silently overwriting the first.
    default_id_consumed = False
    for name, raw in raw_playlists.items():
        if isinstance(raw, dict) and "item_ids" in raw and "items" not in raw:
            items = _playlist_items_from_legacy_item_ids(raw["item_ids"])
        else:
            items = [PlaylistItem.model_validate(i) for i in raw.get("items", [])]
        if name == LEGACY_DEFAULT_PLAYLIST_NAME and not default_id_consumed:
            playlist_id = DEFAULT_PLAYLIST_ID
            default_id_consumed = True
            # Migrate-rename for fleet uniformity: when the dict key is
            # still the legacy "default" string, the operator hasn't
            # renamed it themselves (any UI rename in v2/v3 would have
            # changed the key). Rewrite to "Welcome" so upgraded devices
            # land on the same name as freshly-seeded devices.
            display_name = DEFAULT_PLAYLIST_NAME
        else:
            playlist_id = uuid4()
            display_name = name
        migrated.append(Playlist(id=playlist_id, name=display_name, items=items))
    # Guarantee the default playlist exists so callers can rely on it.
    if not any(p.id == DEFAULT_PLAYLIST_ID for p in migrated):
        migrated.insert(
            0,
            Playlist(id=DEFAULT_PLAYLIST_ID, name=DEFAULT_PLAYLIST_NAME),
        )
    return PlaylistCollection(playlists=migrated), True


def _playlist_items_from_legacy_item_ids(item_ids: list) -> list[PlaylistItem]:
    """v1/v2 → v3+ lift: wrap each raw id in a PlaylistItem with defaults."""
    return [PlaylistItem(item_id=UUID(str(i))) for i in item_ids]


def list_in_playlist_order(
    content_storage: "ContentStorage",
    playlist_storage: PlaylistStorage,
    playlist_id: UUID = DEFAULT_PLAYLIST_ID,
    *,
    include_orphans: bool = False,
) -> list["ContentItem"]:
    """Return content items, playlist order first.

    Default (`include_orphans=False`) returns STRICTLY the items in
    the named playlist — what the playback loop iterates, so bundled
    library assets (seed backgrounds, demo videos) don't leak onto
    the sign unless explicitly added to a playlist.

    With `include_orphans=True`, every item in storage that wasn't
    already returned by the target-playlist loop is appended at the
    end (sorted by id). This includes both items in other playlists
    AND items in no playlist at all — the parameter name is legacy
    from the single-playlist era. The UI pallets + the text-editor's
    background picker depend on this "full library" view, which has
    to surface content from every playlist or non-default playlists
    become invisible once they exist.

    Items referenced by the playlist but missing from storage are
    silently skipped.
    """
    items_by_id = {item.id: item for item in content_storage.list_all()}
    collection = playlist_storage.load_all()
    target = collection.by_id(playlist_id) or Playlist()

    ordered: list[ContentItem] = []
    used: set[UUID] = set()
    for p_item in target.items:
        if p_item.item_id in items_by_id and p_item.item_id not in used:
            # Patch the content item's transition fields with the playlist's
            # values. Since v3 the playlist owns transitions — the content
            # model's fields are legacy-only.
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
        # Everything in storage NOT yet in `ordered` — items in other
        # playlists AND true orphans alike. Pre-2026-04-28 this excluded
        # items in other playlists, which made non-default playlist
        # content invisible to the pallets / bg-picker / api/content.
        extras = [
            item
            for item_id, item in items_by_id.items()
            if item_id not in used
        ]
        extras.sort(key=lambda item: str(item.id))
        ordered.extend(extras)

    return ordered
