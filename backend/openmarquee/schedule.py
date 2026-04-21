"""Time-of-day schedules — rules binding (days, HH:MM windows) → playlist name.

Per SYSTEM_SPEC §5.3, the device supports multiple named playlists with
schedule rules selecting which one is active at any given moment. This
commit ships the data model + persistence + evaluator only; the playback
engine still drives a single global playlist (Phase 5 (a)/(b)/(c)).
Wiring schedules into actual playlist switching needs the multi-playlist
refactor and lands separately.

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
- If no rule matches, `default_playlist_name` is returned.

Timezone contract: the evaluator takes a naive `datetime` and assumes the
device clock is in the operator's local timezone. SYSTEM_SPEC is silent on
this; the device will eventually be configured with `timedatectl set-timezone`
during first-boot. DST transition days are best-effort: spring-forward
silently skips one window and fall-back plays it twice. A future
`Schedule.tz: str | None` field can carry an explicit IANA zone for a
proper-zoned implementation. Reserved as part of the bumpable schema below.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Bump when the on-disk format changes in a non-backward-compatible way.
SCHEDULE_SCHEMA_VERSION = 1

DayOfWeek = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_WEEKDAY_NAMES: tuple[DayOfWeek, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# 00:00-23:59 for start; 00:00-24:00 for end (24:00 = end-of-day shorthand).
_HHMM_START_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_HHMM_END_PATTERN = re.compile(r"^(([01]\d|2[0-3]):([0-5]\d)|24:00)$")

# Playlist names act as foreign keys into the (future) named-playlist store.
# Constrain the format now — every relaxation later is non-breaking; every
# tightening later is a migration.
_PLAYLIST_NAME_PATTERN = r"^[a-z0-9_-]{1,64}$"


class ScheduleRule(BaseModel):
    """A single (days × time-window) → playlist binding.

    TODO(multi-playlist): when named playlists become real entities,
    schedule rules will need a rename-cascade hook (renaming a playlist
    must rewrite schedules) and the evaluator needs a documented
    "playlist_name not found" fallback path.
    """

    name: str = Field(max_length=200, description="Human label, e.g. 'Weekday Lunch'.")
    days: list[DayOfWeek] = Field(min_length=1)
    start_time: str  # HH:MM
    end_time: str  # HH:MM (or "24:00" for end-of-day)
    playlist_name: str = Field(pattern=_PLAYLIST_NAME_PATTERN)
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
    """A list of rules + a default playlist for when none match.

    `schema_version` bumps on non-backward-compatible format changes — see
    `openmarquee.content.storage` for the same migration discipline.

    `tz` is a reserved IANA timezone string (e.g. `America/New_York`) for a
    future zoned evaluator. Today the evaluator uses naive datetime + the
    device's local clock; persisting a tz now means a sign already in the
    field can be upgraded to zoned semantics without losing user intent.
    None means "use the device clock as-is" (current behavior).
    """

    schema_version: int = Field(default=SCHEDULE_SCHEMA_VERSION)
    rules: list[ScheduleRule] = Field(default_factory=list)
    default_playlist_name: str = Field(default="default", pattern=_PLAYLIST_NAME_PATTERN)
    tz: str | None = Field(
        default=None,
        max_length=64,
        description="IANA timezone (e.g. America/New_York). Reserved; not yet enforced.",
    )


def evaluate_schedule(now: datetime, schedule: Schedule) -> str:
    """Return the playlist name active at `now` per the schedule.

    First matching rule wins. Falls back to `default_playlist_name`.
    """
    for rule in schedule.rules:
        if rule.matches(now):
            return rule.playlist_name
    return schedule.default_playlist_name


class ScheduleStorage:
    """Persists the schedule as a single JSON file with atomic writes."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Schedule:
        if not self.path.exists():
            return Schedule()
        data = json.loads(self.path.read_text())
        return Schedule.model_validate(data)

    def save(self, schedule: Schedule) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(schedule.model_dump_json(indent=2))
        tmp.replace(self.path)
