"""Time-of-day schedules — rules binding (days, HH:MM windows) → playlist id.

Per SYSTEM_SPEC §5.3, the device supports multiple playlists with schedule
rules selecting which one is active at any given moment.

Identity: rules reference playlists by stable UUID `playlist_id`, so
renaming a playlist never breaks a schedule reference.

Evaluator semantics:

- Day-of-week match (Mon-Sun). Each rule names which days it applies on.
- Time-of-day window. Half-open [start, end): a 11:00-14:00 rule matches
  11:00:00 through 13:59:59. End < start is allowed and means an overnight
  range (22:00-02:00 matches 22:00-23:59 on the rule's day AND 00:00-01:59
  on the day after).
- `end_time` of "24:00" is accepted as a synonym for end-of-day. Use
  `00:00` to `24:00` for an all-day rule. (`start == end` is an empty
  window and never matches.)
- First matching rule wins — rule order in the schedule is significant.
- If no rule matches, `default_playlist_id` is returned.

Timezone contract: the evaluator takes a naive `datetime` and assumes the
device clock is in the operator's local timezone. SYSTEM_SPEC is silent on
this; the device will eventually be configured with `timedatectl set-timezone`
during first-boot. DST transition days are best-effort: spring-forward
silently skips one window and fall-back plays it twice. A future
`Schedule.tz: str | None` field can carry an explicit IANA zone for a
proper-zoned implementation. Reserved as part of the bumpable schema below.

Storage migration: `schema_version == 1` stored playlist references as
strings (`playlist_name`). On load, those strings get resolved against
the playlist collection's display names — see `migrate_v1_to_v2`. Once
migrated, the file is rewritten in v2 form and references stay UUID-based
across renames.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openmarquee.playlist import (
    DEFAULT_PLAYLIST_ID,
    PlaylistStorage,
)

log = logging.getLogger(__name__)

# Bump when the on-disk format changes in a non-backward-compatible way.
# v1: playlist_name strings.
# v2: playlist_id UUIDs.
SCHEDULE_SCHEMA_VERSION = 2

DayOfWeek = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_WEEKDAY_NAMES: tuple[DayOfWeek, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# 00:00-23:59 for start; 00:00-24:00 for end (24:00 = end-of-day shorthand).
_HHMM_START_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_HHMM_END_PATTERN = re.compile(r"^(([01]\d|2[0-3]):([0-5]\d)|24:00)$")


class ScheduleRule(BaseModel):
    """A single (days × time-window) → playlist binding.

    `playlist_id` references a Playlist's stable UUID. If the referenced
    playlist no longer exists, the playback loop treats the rule as
    matching an empty playlist (renderer goes idle until the next rule
    or the default kicks in).
    """

    name: str = Field(max_length=200, description="Human label, e.g. 'Weekday Lunch'.")
    days: list[DayOfWeek] = Field(min_length=1)
    start_time: str  # HH:MM
    end_time: str  # HH:MM (or "24:00" for end-of-day)
    playlist_id: UUID
    enabled: bool = True

    @field_validator("start_time")
    @classmethod
    def _check_start(cls, value: str) -> str:
        if not _HHMM_START_PATTERN.match(value):
            raise ValueError(f"start_time: expected HH:MM (00:00-23:59), got {value!r}")
        return value

    @field_validator("end_time")
    @classmethod
    def _check_end(cls, value: str) -> str:
        if not _HHMM_END_PATTERN.match(value):
            raise ValueError(f"end_time: expected HH:MM (00:00-23:59) or 24:00, got {value!r}")
        return value

    def matches(self, now: datetime) -> bool:
        """True if this rule is active at `now`.

        Inclusive of `start_time`, exclusive of `end_time`. `end_time == "24:00"`
        is treated as end-of-day. Same-day ranges where `start == end` are
        empty windows and never match. End < start (other than 24:00) is an
        overnight range.
        """
        if not self.enabled:
            return False

        weekday = _WEEKDAY_NAMES[now.weekday()]
        current = f"{now.hour:02d}:{now.minute:02d}"

        # Normalize "24:00" to be greater than any legal current time.
        end_for_compare = self.end_time if self.end_time != "24:00" else "24:00"

        if self.start_time == self.end_time:
            return False  # empty window

        if self.start_time < end_for_compare:
            # Same-day range. Day-of-week check is straightforward.
            return weekday in self.days and self.start_time <= current < end_for_compare

        # Overnight range — two halves: [start, 24:00) on `days`,
        # plus [00:00, end) on the day AFTER each `day`.
        in_first_half = current >= self.start_time and weekday in self.days
        previous_weekday = _WEEKDAY_NAMES[(now.weekday() - 1) % 7]
        in_second_half = current < end_for_compare and previous_weekday in self.days
        return in_first_half or in_second_half


class Schedule(BaseModel):
    """A list of rules + a default playlist id for when none match.

    `tz` is a reserved IANA timezone string for a future zoned evaluator.
    None means "use the device clock as-is" (current behavior).
    """

    # Forward-compat: round-trip unknown fields through the auto-migration
    # rewrite so a future downgrade scenario doesn't silently drop them.
    model_config = ConfigDict(extra="allow")

    schema_version: int = Field(default=SCHEDULE_SCHEMA_VERSION)
    rules: list[ScheduleRule] = Field(default_factory=list)
    default_playlist_id: UUID = Field(default=DEFAULT_PLAYLIST_ID)
    tz: str | None = Field(
        default=None,
        max_length=64,
        description="IANA timezone (e.g. America/New_York). Reserved; not yet enforced.",
    )


def evaluate_schedule(now: datetime, schedule: Schedule) -> UUID:
    """Return the playlist id active at `now` per the schedule.

    First matching rule wins. Falls back to `default_playlist_id`.
    """
    for rule in schedule.rules:
        if rule.matches(now):
            return rule.playlist_id
    return schedule.default_playlist_id


class ScheduleStorage:
    """Persists the schedule as a single JSON file with atomic writes.

    Constructed with an optional `playlist_storage` reference so loads
    can transparently migrate v1 (name-keyed) schedules to v2 (id-keyed)
    by resolving the names against the playlist collection.
    """

    # Perf counters (Batch 6.1). See ContentStorage._stats comment.
    # json_parses (Batch 7.2) separates true re-parses from cache hits.
    _stats: dict[str, int] = {
        "load_calls": 0,
        "save_calls": 0,
        "json_parses": 0,
    }

    def __init__(
        self,
        path: Path,
        playlist_storage: PlaylistStorage | None = None,
    ):
        self.path = Path(path)
        self._playlist_storage = playlist_storage
        # mtime-keyed cache (Batch 7.2). See PlaylistStorage._cache.
        self._cache: tuple[int, Schedule] | None = None

    @classmethod
    def stats_snapshot(cls) -> dict[str, int]:
        return dict(cls._stats)

    def load(self) -> Schedule:
        type(self)._stats["load_calls"] += 1
        if not self.path.exists():
            return Schedule()
        mtime_ns = self.path.stat().st_mtime_ns
        if self._cache is not None and self._cache[0] == mtime_ns:
            return self._cache[1]
        type(self)._stats["json_parses"] += 1
        data = json.loads(self.path.read_text())
        schedule, was_migrated = _coerce_to_schedule(data, self._playlist_storage)
        if was_migrated:
            self.save(schedule)
            mtime_ns = self.path.stat().st_mtime_ns
        self._cache = (mtime_ns, schedule)
        return schedule

    def save(self, schedule: Schedule) -> None:
        type(self)._stats["save_calls"] += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # See PlaylistStorage.save_all comment -- clear cache BEFORE
        # the write so a failed atomic replace doesn't leave stale
        # state in the cache.
        self._cache = None
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(schedule.model_dump_json(indent=2))
        tmp.replace(self.path)


def _coerce_to_schedule(
    data: dict,
    playlist_storage: PlaylistStorage | None,
) -> tuple[Schedule, bool]:
    """Accept the v2 envelope or migrate from v1.

    Returns `(schedule, was_migrated)` so callers can persist the
    upgraded form back to disk.

    v1: playlist references are name strings (`playlist_name`,
    `default_playlist_name`). Resolved via `playlist_storage.load_all()`
    by display name. If a name doesn't resolve, the rule's
    `playlist_id` falls back to DEFAULT_PLAYLIST_ID — the rule will
    play the default until the operator fixes it.
    """
    schema = data.get("schema_version", 1)
    if schema >= SCHEDULE_SCHEMA_VERSION:
        return Schedule.model_validate(data), False

    # v1 → v2 migration. Build a name → id lookup from the playlist
    # collection. If we don't have one (tests passing raw JSON with no
    # playlist storage), fall back to DEFAULT_PLAYLIST_ID for everything.
    name_to_id: dict[str, UUID] = {}
    if playlist_storage is not None:
        collection = playlist_storage.load_all()
        for p in collection.playlists:
            # First-write-wins on duplicate names — `by_name` semantics.
            name_to_id.setdefault(p.name, p.id)

    def resolve(name: str | None, *, context: str) -> UUID:
        if name and name in name_to_id:
            return name_to_id[name]
        if name:
            # Operator visibility: if a v1 rule references a playlist
            # that no longer exists by name, log it. The migrated
            # schedule still loads (rule routes to default), but the
            # log line tells the operator which rules need attention.
            log.warning(
                "schedule v1→v2 migration: %s references playlist_name=%r "
                "which doesn't resolve; falling back to DEFAULT_PLAYLIST_ID",
                context,
                name,
            )
        return DEFAULT_PLAYLIST_ID

    migrated_rules = []
    for raw in data.get("rules", []):
        rule_data = dict(raw)
        legacy_name = rule_data.pop("playlist_name", None)
        rule_data["playlist_id"] = resolve(legacy_name, context=f"rule {raw.get('name', '?')!r}")
        migrated_rules.append(ScheduleRule.model_validate(rule_data))

    return (
        Schedule(
            rules=migrated_rules,
            default_playlist_id=resolve(
                data.get("default_playlist_name"), context="default_playlist_name"
            ),
            tz=data.get("tz"),
        ),
        True,
    )
