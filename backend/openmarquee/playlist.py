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
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field, model_validator

from openmarquee._atomic import atomic_write_text
from openmarquee._storage_recovery import quarantine_corrupt_file

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
# to "Welcome" 2026-04-28 to match the seeded slide content; renamed again
# to "Demo" 2026-05-06 when the FREE YOUR SIGN reel landed as the
# self-running first-boot demo. The changes are name-only — the
# playlist's identity is its UUID, so schedule rules / API references
# keep working across the renames.
DEFAULT_PLAYLIST_NAME = "Demo"
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
        "shutter",
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

        P2 (2026-05-21): `item_ids` is POPPED whenever present — not only
        when `items` is absent. `item_ids` is a `@computed_field`, so it
        is re-emitted on every dump; combined with `extra="allow"`, a
        persisted `item_ids` key would otherwise be captured as an extra
        field and written a SECOND time on the next save — a duplicate
        JSON key. When `items` is also present it is authoritative and
        the `item_ids` echo is simply discarded; only a legacy object
        with `item_ids` and no `items` is migrated.
        """
        if isinstance(data, dict) and "item_ids" in data:
            ids = data.pop("item_ids")
            if "items" not in data:
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
            "shutter",
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

    # Perf counters (Batch 6.1). See ContentStorage._stats comment.
    # json_parses (Batch 7.2) separates true on-disk re-parses from
    # cache-hit returns so the sweep diff can show the cache working.
    _stats: dict[str, int] = {
        "load_all_calls": 0,
        "save_all_calls": 0,
        "json_parses": 0,
    }

    def __init__(self, path: Path):
        self.path = Path(path)
        # mtime-keyed cache (Batch 7.2). See ContentStorage._cache
        # rationale; same instance-level / singleton-at-runtime
        # design + read-only-by-contract caveat. Skip json.loads +
        # _coerce_to_collection (Pydantic validate over every
        # Playlist + every PlaylistItem) when the file hasn't moved.
        self._cache: tuple[int, PlaylistCollection] | None = None
        # Round-27 concurrency: serialize the load+mutate+save trios
        # in set_by_id / delete_by_id / prune_dangling_refs / the new
        # append_item_to_default / remove_item_from_default. FastAPI
        # sync handlers run on a threadpool; two concurrent edits
        # (e.g. Tab 1 PUT /api/playlists/{id} drag-reorder + Tab 2
        # POST /api/content/images that triggers server-side
        # _append_to_playlist) would otherwise interleave their
        # load+save and lose one operator's edit on disk.
        # Operator-visible: slides created during a drag-reorder
        # window vanish from the playlist (content envelope still
        # on disk, just unreferenced). Permanent corruption,
        # invisible until somebody notices.
        #
        # Same threading.Lock vs asyncio.Lock caveat as
        # TombstoneStorage (r25 5e0fcf4) and FlockStorage (also
        # r25): the mutator methods are sync and may be called from
        # async paths, where the lock briefly blocks the event loop
        # during contention. Acceptable for microsecond-scale ops;
        # asyncio.Lock would cascade through every caller.
        #
        # IMPORTANT: threading.Lock is NOT reentrant. The mutator
        # methods below do load_all + mutate + save_all inside the
        # lock; they MUST NOT call other locked methods (which would
        # double-acquire and deadlock). Specifically:
        #   - set_by_id / delete_by_id / prune_dangling_refs each do
        #     their own load_all+save_all under the lock.
        #   - append_item_to_default / remove_item_from_default
        #     bypass set_by_id and use load_all+save_all directly.
        #   - The legacy save() (line ~399) calls set_by_id; it
        #     stays UNLOCKED at its own level so the inner
        #     set_by_id acquires once (no reentry).
        self._lock = threading.Lock()

    @classmethod
    def stats_snapshot(cls) -> dict[str, int]:
        return dict(cls._stats)

    # --- collection primitives ---

    def load_all(self) -> PlaylistCollection:
        """Return the full collection. Migrates legacy formats on the fly
        (v1/v2/v3 → v4) AND rewrites the file on a successful migration
        so subsequent loads skip the conversion."""
        type(self)._stats["load_all_calls"] += 1
        if not self.path.exists():
            return _bootstrap_default_collection()
        mtime_ns = self.path.stat().st_mtime_ns
        if self._cache is not None and self._cache[0] == mtime_ns:
            return self._cache[1]
        type(self)._stats["json_parses"] += 1
        # 19.2 / sweep #10 #4: on JSON corruption or schema mismatch,
        # quarantine the bad file + start fresh with defaults. The
        # next save_all() overwrites the original path. Without this,
        # a power-loss-during-write or a hand-edit gone wrong leaves
        # the backend hard-crashing on every restart.
        try:
            data = json.loads(self.path.read_text())
            collection, was_migrated = _coerce_to_collection(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            quarantine_corrupt_file(self.path, exc)
            return _bootstrap_default_collection()
        if was_migrated:
            self.save_all(collection)
            mtime_ns = self.path.stat().st_mtime_ns
        self._cache = (mtime_ns, collection)
        return collection

    def save_all(self, collection: PlaylistCollection) -> None:
        """Atomically write the full collection to disk."""
        type(self)._stats["save_all_calls"] += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Drop the cache BEFORE the write so an IO error (disk full,
        # permission) doesn't leave the cache holding a mutated-but-
        # unpersisted object. Some callers (set_by_id, delete_by_id)
        # mutate the load_all() return in-place before calling save_all
        # -- without this, a failed write would let next load_all()
        # return the mutated cached value that doesn't match disk.
        self._cache = None
        # 11.2: atomic_write_text sets 0600 + cleans up orphan
        # .tmp on rollback. Centralized in openmarquee._atomic.
        atomic_write_text(self.path, collection.model_dump_json(indent=2))

    def get_by_id(self, playlist_id: UUID) -> Playlist | None:
        """Return the playlist with the given id, or None."""
        return self.load_all().by_id(playlist_id)

    def set_by_id(self, playlist: Playlist) -> None:
        """Create or replace a playlist by id. The playlist's `id` field
        is the lookup key; `name` and `items` are the writable fields.

        Round-27: lock-protected load+mutate+save (see __init__ for
        the race rationale).
        """
        with self._lock:
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

        Round-27: lock-protected.
        """
        with self._lock:
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

        Round-27: lock-protected.
        """
        with self._lock:
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

    def append_item_to_default(self, item_id: UUID) -> None:
        """Idempotent append of `item_id` to the default playlist.

        Round-27: moved INTO PlaylistStorage from api.py's
        _append_to_playlist helper. The helper did
        `load() + append + save()` -- three separate calls -- which
        couldn't be lock-protected at the call layer without exposing
        every API caller to the race. Moving the trio into one
        lock-protected method here closes the race + makes future
        callers unable to bypass the lock by mistake.

        Behavior preserved: creates a fresh default playlist if none
        exists (DEFAULT_PLAYLIST_ID + DEFAULT_PLAYLIST_NAME), appends
        the item id, persists.
        """
        with self._lock:
            collection = self.load_all()
            target = collection.by_id(DEFAULT_PLAYLIST_ID)
            if target is None:
                target = Playlist(id=DEFAULT_PLAYLIST_ID, name=DEFAULT_PLAYLIST_NAME)
                collection.playlists.append(target)
            target.append(item_id)
            self.save_all(collection)

    def remove_item_from_default(self, item_id: UUID) -> None:
        """Idempotent remove of `item_id` from the default playlist.

        Round-27: moved INTO PlaylistStorage from api.py's
        _remove_from_playlist helper (same race rationale as
        append_item_to_default above). Behavior preserved: removes
        only from the default playlist; OTHER playlists referencing
        the same item are unchanged (this matches the pre-r27
        helper's exact behavior -- a separate cleanup sweep across
        all playlists is out of scope for this concurrency fix).
        """
        with self._lock:
            collection = self.load_all()
            target = collection.by_id(DEFAULT_PLAYLIST_ID)
            if target is None:
                return
            target.remove(item_id)
            self.save_all(collection)

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


# Forward-compat carve-out for the v1/v2/v3 migration paths. Every key
# this function explicitly consumes or that PlaylistCollection declares
# as a field MUST be listed here so the `**extras` splat at the v1/v2/
# v3 returns doesn't shadow an explicit kwarg or carry a legacy key
# forward as a model_extra. `item_ids` is the v1 legacy top-level the
# migration converts to a Playlist; v2/v3 only consume `playlists`
# (already in model_fields). The set mirrors schedule.py's
# MIGRATION_HANDLED_TOP_LEVEL pattern.
MIGRATION_HANDLED_TOP_LEVEL = frozenset(PlaylistCollection.model_fields.keys()) | {"item_ids"}


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
    if (
        schema >= PLAYLIST_SCHEMA_VERSION
        and "playlists" in data
        and isinstance(data["playlists"], list)
    ):
        return PlaylistCollection.model_validate(data), False

    # v1: unnamed single playlist.
    if "item_ids" in data and "playlists" not in data:
        legacy = _playlist_items_from_legacy_item_ids(data.get("item_ids", []))
        # Forward-compat: splat unknown top-level fields through **extras
        # so a hypothetical future-v5 field on a v1 file survives migration.
        # See MIGRATION_HANDLED_TOP_LEVEL comment for the carve-out rationale.
        extras = {k: v for k, v in data.items() if k not in MIGRATION_HANDLED_TOP_LEVEL}
        return (
            PlaylistCollection(
                playlists=[
                    Playlist(
                        id=DEFAULT_PLAYLIST_ID,
                        name=DEFAULT_PLAYLIST_NAME,
                        items=legacy,
                    )
                ],
                **extras,
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
    # Forward-compat: same **extras splat as the v1 path -- unknown
    # top-level fields on a v2/v3 file survive migration.
    extras = {k: v for k, v in data.items() if k not in MIGRATION_HANDLED_TOP_LEVEL}
    return PlaylistCollection(playlists=migrated, **extras), True


def _playlist_items_from_legacy_item_ids(item_ids: list) -> list[PlaylistItem]:
    """v1/v2 → v3+ lift: wrap each raw id in a PlaylistItem with defaults."""
    return [PlaylistItem(item_id=UUID(str(i))) for i in item_ids]


def _playlist_ordered_prefix(
    content_storage: "ContentStorage",
    playlist_storage: PlaylistStorage,
    playlist_id: UUID,
) -> tuple[list["ContentItem"], set[UUID], dict[UUID, "ContentItem"]]:
    """Build the playlist-ordered prefix shared by both list_for_playback
    and list_full_library. Returns (ordered_so_far, used_ids, items_by_id)
    so the full-library composition can append remaining items without
    re-walking storage.

    Items referenced by the playlist but missing from storage are
    silently skipped -- load-bearing forgiveness so an operator can
    `rm -rf content/<uuid>/` without the renderer KeyError'ing on
    the next fetch.

    Patches each yielded item's transition + transition_ms from the
    PlaylistItem since v3 (the content model's transition fields are
    legacy-only).
    """
    items_by_id = {item.id: item for item in content_storage.list_all()}
    collection = playlist_storage.load_all()
    target = collection.by_id(playlist_id) or Playlist()

    ordered: list[ContentItem] = []
    used: set[UUID] = set()
    for p_item in target.items:
        if p_item.item_id in items_by_id and p_item.item_id not in used:
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
    return ordered, used, items_by_id


def list_for_playback(
    content_storage: "ContentStorage",
    playlist_storage: PlaylistStorage,
    playlist_id: UUID = DEFAULT_PLAYLIST_ID,
) -> list["ContentItem"]:
    """STRICT: items in `playlist_id` only, in playlist order, with
    transitions patched from PlaylistItem.

    What the playback loop iterates -- bundled library assets (seed
    backgrounds, demo videos) don't leak onto the sign unless they're
    explicitly added to a playlist.

    Stale playlist refs (PlaylistItem.item_id with no matching content
    in storage) are silently skipped. See _playlist_ordered_prefix.
    """
    ordered, _used, _items_by_id = _playlist_ordered_prefix(
        content_storage, playlist_storage, playlist_id
    )
    return ordered


def list_full_library(
    content_storage: "ContentStorage",
    playlist_storage: PlaylistStorage,
    anchor_playlist_id: UUID = DEFAULT_PLAYLIST_ID,
) -> list["ContentItem"]:
    """UI library view: anchor playlist first (playlist-ordered, with
    transitions patched), then every other storage item appended in
    chronological order by created_at + id tiebreak.

    The trailing items include BOTH content in non-anchor playlists
    AND true orphans (content in no playlist at all). The UI pallets +
    the text-editor's background picker depend on this "everything in
    storage shows up" view -- pre-2026-04-28 this excluded other-
    playlist content, which made non-default playlists invisible to
    /api/content the moment a second playlist existed.

    Chronological tail order (B19, 2026-05-05) so newly-created items
    land at the end. The frontend slide-browser re-sorts for its own
    display, but other API consumers see the creation-order intent.
    """
    ordered, used, items_by_id = _playlist_ordered_prefix(
        content_storage, playlist_storage, anchor_playlist_id
    )
    extras = [item for item_id, item in items_by_id.items() if item_id not in used]
    extras.sort(key=lambda item: (str(item.created_at or ""), str(item.id)))
    ordered.extend(extras)
    return ordered
