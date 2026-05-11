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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field

from openmarquee._atomic import atomic_write_text

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


class TombstoneLog(BaseModel):
    """Envelope wrapping the tombstone list + schema version."""

    schema_version: int = TOMBSTONE_SCHEMA_VERSION
    tombstones: list[Tombstone] = Field(default_factory=list)


class TombstoneStorage:
    """Atomic file-backed persistence for the tombstone log."""

    def __init__(self, path: Path, ttl_days: int = TOMBSTONE_TTL_DAYS):
        self.path = Path(path)
        self.ttl_days = ttl_days

    def load(self) -> TombstoneLog:
        if not self.path.exists():
            return TombstoneLog()
        data = json.loads(self.path.read_text())
        return TombstoneLog.model_validate(data)

    def save(self, log: TombstoneLog) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 11.2: atomic_write_text sets 0600 + cleans up orphan .tmp.
        atomic_write_text(self.path, log.model_dump_json(indent=2))

    def add(self, content_id: UUID, *, now: datetime | None = None) -> Tombstone:
        """Record a deletion. If a tombstone for this id already exists the
        timestamp is refreshed (so TTL counts from the most recent delete)."""
        when = now or _now()
        log = self.load()
        log.tombstones = [t for t in log.tombstones if t.content_id != content_id]
        stone = Tombstone(content_id=content_id, deleted_at=when)
        log.tombstones.append(stone)
        self.save(log)
        return stone

    def list_active(self, *, now: datetime | None = None) -> list[Tombstone]:
        """Return tombstones still within the TTL window. Expired entries
        are filtered out but not pruned from disk — callers that want to
        compact storage should call prune_expired()."""
        cutoff = (now or _now()) - timedelta(days=self.ttl_days)
        return [t for t in self.load().tombstones if t.deleted_at >= cutoff]

    def prune_expired(self, *, now: datetime | None = None) -> int:
        """Drop expired tombstones from disk. Returns the number removed."""
        cutoff = (now or _now()) - timedelta(days=self.ttl_days)
        log = self.load()
        before = len(log.tombstones)
        log.tombstones = [t for t in log.tombstones if t.deleted_at >= cutoff]
        removed = before - len(log.tombstones)
        if removed:
            self.save(log)
        return removed
