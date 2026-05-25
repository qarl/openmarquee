"""Tombstone log — a short-lived record of recently-deleted content_ids.

A peer that pulls our manifest after we've deleted something needs to
know the delete happened so it can apply the same delete locally —
otherwise the content would resurrect on the next sync round. The
tombstone log is that "you just missed it" breadcrumb trail.

Entries age out after `TOMBSTONE_TTL_DAYS` so the log doesn't grow
unbounded. TTL is the window in which a peer can be offline and still
learn about a deletion; a peer absent longer than that will re-sync
the content (the conservative direction — resurrect is annoying,
silent data loss is worse).

Storage shape (tombstones.json):

    {
      "schema_version": 1,
      "tombstones": [
        {"content_id": "<uuid>", "deleted_at": "2026-04-24T..."},
        ...
      ]
    }
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError, field_validator

from openmarquee._atomic import atomic_write_text
from openmarquee._storage_recovery import quarantine_corrupt_file

TOMBSTONE_SCHEMA_VERSION = 1
# All peers must agree on this value — a peer with a shorter TTL stops
# advertising a deletion before a longer-TTL peer has caught up, which
# resurrects the content. Treat this as part of the flock wire contract,
# not a per-device knob.
TOMBSTONE_TTL_DAYS = 30


def _now() -> datetime:
    return datetime.now(UTC)


class Tombstone(BaseModel):
    """One deletion breadcrumb."""

    content_id: UUID
    deleted_at: datetime

    @field_validator("deleted_at", mode="after")
    @classmethod
    def _ensure_aware_utc(cls, value: datetime) -> datetime:
        """Round-24 correctness fix: coerce naive datetimes to aware
        UTC.

        TombstoneStorage.save() always writes aware UTC (model_dump_
        json on aware datetime). So a naive deleted_at can only land
        in tombstones.json via external corruption: restore from a
        backup tool that strips offsets, operator hand-edit, or
        import from an older/peer device that wrote naive.

        Pre-fix the bare `datetime` field accepted naive ISO without
        raising, so quarantine_corrupt_file never triggered. Then
        list_active / prune_expired's `t.deleted_at >= cutoff` (cutoff
        is aware UTC from `_now() - timedelta(...)`) raised
        TypeError("can't compare offset-naive and offset-aware
        datetimes"). Every subsequent sync round AND prune call 500'd
        silently. Deletions stopped propagating; the log grew
        unbounded until a human investigated.

        Coerce-to-UTC is safer than raising -- survives the data and
        keeps sync working. Reasonable interpretation: "if you wrote
        a naive ISO, you meant UTC." If the value is already aware,
        pass through unchanged (no tz conversion).
        """
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class TombstoneLog(BaseModel):
    """Envelope wrapping the tombstone list + schema version."""

    schema_version: int = TOMBSTONE_SCHEMA_VERSION
    tombstones: list[Tombstone] = Field(default_factory=list)


class TombstoneStorage:
    """Atomic file-backed persistence for the tombstone log."""

    def __init__(self, path: Path, ttl_days: int = TOMBSTONE_TTL_DAYS):
        self.path = Path(path)
        self.ttl_days = ttl_days
        # Round-25 concurrency: serialize the load+mutate+save trios
        # in add/remove/prune_expired. FastAPI sync handlers run on a
        # threadpool, so two concurrent DELETE /api/content/{id}
        # requests (operator batch-selecting + deleting rapidly)
        # could interleave their load+save and lose one tombstone --
        # the missing tombstone means peers don't learn of the
        # delete on the next sync round, and the content silently
        # RESURRECTS in the operator's playlist when pulled back
        # from a peer that still has it. Same failure mode flock_
        # sync hardened against in r9/r16/r20-22.
        #
        # threading.Lock (vs asyncio.Lock) caveat: the mutator
        # methods are sync and called both from sync handlers
        # (correct) AND from async paths (flock_sync._ingest_delete
        # /_apply_pulled_tombstone are async methods that call
        # these sync mutators). Acquiring threading.Lock from an
        # async coroutine blocks the event loop briefly during
        # contention -- acceptable because tombstone ops are
        # microseconds (in-memory list mutate + sync file write),
        # and uncontended-acquire is ~free. Switching to asyncio.
        # Lock would require sync→async on all mutator signatures
        # + cascade through every caller; separate refactor if a
        # future profile shows event-loop stalls here.
        self._lock = threading.Lock()

    def load(self) -> TombstoneLog:
        if not self.path.exists():
            return TombstoneLog()
        # 19.2 / sweep #10 #4: see PlaylistStorage.load_all note.
        try:
            data = json.loads(self.path.read_text())
            return TombstoneLog.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            quarantine_corrupt_file(self.path, exc)
            return TombstoneLog()

    def save(self, log: TombstoneLog) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 11.2: atomic_write_text sets 0600 + cleans up orphan .tmp.
        atomic_write_text(self.path, log.model_dump_json(indent=2))

    def add(self, content_id: UUID, *, now: datetime | None = None) -> Tombstone:
        """Record a deletion. If a tombstone for this id already exists the
        timestamp is refreshed (so TTL counts from the most recent delete).

        Round-25: lock-protected load+mutate+save. See __init__ for the
        race rationale (concurrent DELETE /api/content/{id} would
        otherwise lose tombstones → content resurrects via peer sync).
        """
        when = now or _now()
        with self._lock:
            log = self.load()
            log.tombstones = [t for t in log.tombstones if t.content_id != content_id]
            stone = Tombstone(content_id=content_id, deleted_at=when)
            log.tombstones.append(stone)
            self.save(log)
        return stone

    def remove(self, content_id: UUID) -> bool:
        """Drop a tombstone if present. Returns True if removed, False if
        absent.

        Round-16 rollback support: delete_content_item adds the tombstone
        BEFORE the destructive storage.delete + playlist-remove steps
        (so a tombstone-add failure aborts cleanly without losing the
        delete intent). When a subsequent step fails, the caller calls
        remove() to roll the tombstone back so a retry can restart from
        scratch -- without rollback, the tombstone is committed while
        the local asset/envelope + playlist refs remain stale forever.

        Round-25: lock-protected load+mutate+save (same rationale as
        add()).
        """
        with self._lock:
            log = self.load()
            before = len(log.tombstones)
            log.tombstones = [t for t in log.tombstones if t.content_id != content_id]
            if len(log.tombstones) == before:
                return False
            self.save(log)
        return True

    def list_active(self, *, now: datetime | None = None) -> list[Tombstone]:
        """Return tombstones still within the TTL window. Expired entries
        are filtered out but not pruned from disk — callers that want to
        compact storage should call prune_expired()."""
        cutoff = (now or _now()) - timedelta(days=self.ttl_days)
        return [t for t in self.load().tombstones if t.deleted_at >= cutoff]

    def prune_expired(self, *, now: datetime | None = None) -> int:
        """Drop expired tombstones from disk. Returns the number removed.

        Round-25: lock-protected load+mutate+save (same rationale as
        add()).
        """
        cutoff = (now or _now()) - timedelta(days=self.ttl_days)
        with self._lock:
            log = self.load()
            before = len(log.tombstones)
            log.tombstones = [t for t in log.tombstones if t.deleted_at >= cutoff]
            removed = before - len(log.tombstones)
            if removed:
                self.save(log)
        return removed
