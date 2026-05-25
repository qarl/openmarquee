import json
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from openmarquee.playlist import (
    DEFAULT_PLAYLIST_ID,
    Playlist,
    PlaylistStorage,
)
from openmarquee.schedule import (
    SCHEDULE_SCHEMA_VERSION,
    Schedule,
    ScheduleRule,
    ScheduleStorage,
    _coerce_to_schedule,
    _resolve_legacy_name,
    evaluate_schedule,
)

# A handful of pre-allocated UUIDs to make assertions readable.
PL_LUNCH = UUID("00000000-0000-4000-8000-000000000010")
PL_WORKDAY = UUID("00000000-0000-4000-8000-000000000011")
PL_WEEKEND = UUID("00000000-0000-4000-8000-000000000012")


# --- ScheduleRule field validation ---


def test_rule_requires_at_least_one_day():
    with pytest.raises(ValidationError):
        ScheduleRule(
            name="x",
            days=[],
            start_time="08:00",
            end_time="17:00",
            playlist_id=PL_LUNCH,
        )


def test_rule_rejects_unknown_day():
    with pytest.raises(ValidationError):
        ScheduleRule(
            name="x",
            days=["funday"],  # type: ignore[list-item]
            start_time="08:00",
            end_time="17:00",
            playlist_id=PL_LUNCH,
        )


def test_rule_rejects_malformed_time():
    with pytest.raises(ValidationError):
        ScheduleRule(
            name="x",
            days=["mon"],
            start_time="8:00",
            end_time="17:00",
            playlist_id=PL_LUNCH,
        )


def test_rule_rejects_out_of_range_time():
    with pytest.raises(ValidationError):
        ScheduleRule(
            name="x",
            days=["mon"],
            start_time="24:00",
            end_time="25:00",
            playlist_id=PL_LUNCH,
        )


# --- ScheduleRule.matches ---


def _rule(days, start, end, name="r", playlist_id=PL_LUNCH):
    return ScheduleRule(
        name=name,
        days=days,
        start_time=start,
        end_time=end,
        playlist_id=playlist_id,
    )


def test_matches_returns_false_for_wrong_day():
    rule = _rule(["mon"], "08:00", "17:00")
    # 2026-04-21 is a Tuesday.
    assert rule.matches(datetime(2026, 4, 21, 12, 0)) is False


def test_matches_returns_true_inside_window():
    rule = _rule(["mon", "tue"], "08:00", "17:00")
    assert rule.matches(datetime(2026, 4, 21, 12, 0)) is True


def test_matches_inclusive_of_start_time():
    rule = _rule(["tue"], "08:00", "17:00")
    assert rule.matches(datetime(2026, 4, 21, 8, 0)) is True


def test_matches_exclusive_of_end_time():
    rule = _rule(["tue"], "08:00", "17:00")
    assert rule.matches(datetime(2026, 4, 21, 17, 0)) is False


def test_overnight_window_matches_late_evening():
    rule = _rule(["fri"], "22:00", "02:00")
    # Friday 23:30
    assert rule.matches(datetime(2026, 4, 24, 23, 30)) is True


def test_overnight_window_matches_early_morning_on_following_day():
    rule = _rule(["fri"], "22:00", "02:00")
    # Saturday 01:30 — still in the Friday-night window
    assert rule.matches(datetime(2026, 4, 25, 1, 30)) is True


def test_overnight_window_does_not_match_outside_either_half():
    rule = _rule(["fri"], "22:00", "02:00")
    # Saturday 03:00 — past the end
    assert rule.matches(datetime(2026, 4, 25, 3, 0)) is False
    # Saturday 21:00 — wrong day for the start half
    assert rule.matches(datetime(2026, 4, 25, 21, 0)) is False


# --- evaluate_schedule ---


def test_empty_schedule_returns_default_playlist():
    schedule = Schedule(default_playlist_id=DEFAULT_PLAYLIST_ID)
    assert evaluate_schedule(datetime(2026, 4, 21, 12, 0), schedule) == DEFAULT_PLAYLIST_ID


def test_first_matching_rule_wins():
    schedule = Schedule(
        rules=[
            _rule(["mon", "tue"], "11:00", "14:00", playlist_id=PL_LUNCH),
            _rule(["mon", "tue"], "08:00", "17:00", playlist_id=PL_WORKDAY),
        ],
        default_playlist_id=DEFAULT_PLAYLIST_ID,
    )
    # Tuesday 12:00 matches both — the lunch rule wins because it's first.
    assert evaluate_schedule(datetime(2026, 4, 21, 12, 0), schedule) == PL_LUNCH
    # Tuesday 09:00 only matches workday.
    assert evaluate_schedule(datetime(2026, 4, 21, 9, 0), schedule) == PL_WORKDAY


def test_no_matching_rule_falls_back_to_default():
    schedule = Schedule(
        rules=[_rule(["mon"], "08:00", "17:00", playlist_id=PL_WORKDAY)],
        default_playlist_id=DEFAULT_PLAYLIST_ID,
    )
    # Tuesday — wrong day for the only rule.
    assert evaluate_schedule(datetime(2026, 4, 21, 12, 0), schedule) == DEFAULT_PLAYLIST_ID


# --- ScheduleStorage ---


def test_empty_load_returns_empty_schedule(tmp_path: Path):
    storage = ScheduleStorage(tmp_path / "schedules.json")
    schedule = storage.load()
    assert schedule.rules == []
    assert schedule.default_playlist_id == DEFAULT_PLAYLIST_ID


def test_save_then_load_round_trips(tmp_path: Path):
    storage = ScheduleStorage(tmp_path / "schedules.json")
    rule = _rule(["sat", "sun"], "00:00", "23:59", name="weekend", playlist_id=PL_WEEKEND)
    schedule = Schedule(rules=[rule], default_playlist_id=PL_WORKDAY)
    storage.save(schedule)

    loaded = storage.load()
    assert loaded.default_playlist_id == PL_WORKDAY
    assert len(loaded.rules) == 1
    assert loaded.rules[0].playlist_id == PL_WEEKEND


def test_atomic_write_leaves_no_tmp_files(tmp_path: Path):
    storage = ScheduleStorage(tmp_path / "schedules.json")
    storage.save(Schedule())
    assert list(tmp_path.glob("*.tmp")) == []


# --- v1 → v2 migration (playlist_name strings → playlist_id UUIDs) ---


def test_v1_legacy_name_strings_migrate_to_ids(tmp_path: Path):
    """A v1 schedule with playlist_name strings on disk should migrate
    transparently when a playlist storage is wired in."""
    # Set up a playlist collection with a "lunch" playlist.
    playlist_storage = PlaylistStorage(tmp_path / "playlist.json")
    lunch = Playlist(name="lunch")
    lunch.append(uuid4())
    playlist_storage.set_by_id(lunch)

    # Write a v1 schedule referring to "lunch" by name.
    schedule_path = tmp_path / "schedules.json"
    schedule_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rules": [
                    {
                        "name": "weekday lunch",
                        "days": ["mon", "tue"],
                        "start_time": "11:00",
                        "end_time": "14:00",
                        "playlist_name": "lunch",
                        "enabled": True,
                    }
                ],
                "default_playlist_name": "default",
            }
        )
    )

    storage = ScheduleStorage(schedule_path, playlist_storage=playlist_storage)
    loaded = storage.load()
    assert loaded.rules[0].playlist_id == lunch.id
    assert loaded.default_playlist_id == DEFAULT_PLAYLIST_ID


def test_v1_unresolvable_name_falls_back_to_default(tmp_path: Path):
    """A rule referencing a nonexistent playlist name during migration
    should resolve to DEFAULT_PLAYLIST_ID — better than crashing on boot."""
    playlist_storage = PlaylistStorage(tmp_path / "playlist.json")
    # Default playlist exists (auto-bootstrapped) but no others.
    schedule_path = tmp_path / "schedules.json"
    schedule_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rules": [
                    {
                        "name": "promo",
                        "days": ["mon"],
                        "start_time": "08:00",
                        "end_time": "17:00",
                        "playlist_name": "ghost-playlist",
                        "enabled": True,
                    }
                ],
                "default_playlist_name": "default",
            }
        )
    )
    storage = ScheduleStorage(schedule_path, playlist_storage=playlist_storage)
    loaded = storage.load()
    assert loaded.rules[0].playlist_id == DEFAULT_PLAYLIST_ID


def test_v1_migration_with_playlist_storage_none_routes_everything_to_default():
    """The `if playlist_storage is not None` branch at schedule.py:248.

    When _coerce_to_schedule is called with no playlist storage (tests
    passing raw JSON), name_to_id stays empty so every legacy name
    resolves to DEFAULT_PLAYLIST_ID. Exercises the branch every
    storage-backed test skips.
    """
    data = {
        "schema_version": 1,
        "rules": [
            {
                "name": "morning",
                "days": ["mon", "tue"],
                "start_time": "08:00",
                "end_time": "12:00",
                "playlist_name": "lunch",
                "enabled": True,
            },
            {
                "name": "afternoon",
                "days": ["wed"],
                "start_time": "13:00",
                "end_time": "17:00",
                "playlist_name": "promo",
                "enabled": True,
            },
        ],
        "default_playlist_name": "default",
    }
    schedule, was_migrated = _coerce_to_schedule(data, playlist_storage=None)
    assert was_migrated is True
    assert len(schedule.rules) == 2
    assert all(r.playlist_id == DEFAULT_PLAYLIST_ID for r in schedule.rules)
    assert schedule.default_playlist_id == DEFAULT_PLAYLIST_ID


def test_v1_unknown_default_playlist_name_logs_and_falls_back(tmp_path: Path, caplog):
    """Symmetric to test_v1_unresolvable_name_falls_back_to_default but
    pins the `default_playlist_name` branch at schedule.py:281 (was
    uncovered: the existing unknown-name test only covered the rule
    path)."""
    playlist_storage = PlaylistStorage(tmp_path / "playlist.json")
    schedule_path = tmp_path / "schedules.json"
    schedule_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rules": [],
                "default_playlist_name": "ghost-default",
            }
        )
    )
    storage = ScheduleStorage(schedule_path, playlist_storage=playlist_storage)
    with caplog.at_level("WARNING", logger="openmarquee.schedule"):
        loaded = storage.load()
    assert loaded.default_playlist_id == DEFAULT_PLAYLIST_ID
    # The warning fires with context="default_playlist_name" and the
    # unresolved name in the message.
    matching = [
        r
        for r in caplog.records
        if "default_playlist_name" in r.getMessage() and "ghost-default" in r.getMessage()
    ]
    assert len(matching) == 1, (
        f"expected exactly one default_playlist_name warning, got {len(matching)}"
    )


def test_v1_migration_persists_to_disk(tmp_path: Path):
    """After storage.load() triggers v1→v2 migration, the file on disk
    must be rewritten as v2 — otherwise the next boot re-migrates,
    operator-visible legacy keys linger, and a regression could leave
    v1 files in v1-form forever while in-memory state looks fine."""
    playlist_storage = PlaylistStorage(tmp_path / "playlist.json")
    lunch = Playlist(name="lunch")
    lunch.append(uuid4())
    playlist_storage.set_by_id(lunch)

    schedule_path = tmp_path / "schedules.json"
    schedule_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rules": [
                    {
                        "name": "weekday lunch",
                        "days": ["mon"],
                        "start_time": "11:00",
                        "end_time": "14:00",
                        "playlist_name": "lunch",
                        "enabled": True,
                    }
                ],
                "default_playlist_name": "default",
            }
        )
    )
    storage = ScheduleStorage(schedule_path, playlist_storage=playlist_storage)
    storage.load()

    # Re-read the file directly (not via storage) and assert the
    # on-disk shape is v2.
    on_disk = json.loads(schedule_path.read_text())
    assert on_disk["schema_version"] == 2
    # Legacy keys must NOT survive the rewrite.
    assert "default_playlist_name" not in on_disk
    for rule in on_disk["rules"]:
        assert "playlist_name" not in rule
        assert "playlist_id" in rule


def test_v1_migration_drops_unknown_top_level_fields(tmp_path: Path):
    """The v1→v2 migration explicitly constructs Schedule from named
    fields (rules / default_playlist_id / tz), so any extra top-level
    field present in a v1 payload is DROPPED — model_config's
    extra="allow" preserves extras only through model_validate, not
    through direct __init__. Pin this behavior so a future refactor
    that switches the migration to a "preserve-extras-then-validate"
    shape (or vice-versa) surfaces here as a tripwire.

    If the migration is ever rebuilt to preserve extras (which would
    make forward-compat extras survive a v1 load + save round-trip),
    this test should flip to assert survival rather than be deleted.
    """
    playlist_storage = PlaylistStorage(tmp_path / "playlist.json")
    schedule_path = tmp_path / "schedules.json"
    schedule_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rules": [],
                "default_playlist_name": "default",
                "future_v3_field": "hypothetical-forward-compat-value",
            }
        )
    )
    storage = ScheduleStorage(schedule_path, playlist_storage=playlist_storage)
    storage.load()

    on_disk = json.loads(schedule_path.read_text())
    # Document the CURRENT behavior: v1 migration drops extras.
    assert "future_v3_field" not in on_disk, (
        "v1→v2 migration currently constructs Schedule from named "
        "fields only; extras are lost. If this assertion flips, the "
        "migration was rebuilt to preserve extras — update the "
        "assertion to assert survival."
    )


# --- _resolve_legacy_name (hoisted from _coerce_to_schedule) ---


def test_resolve_legacy_name_returns_mapped_id_for_known_name(caplog):
    """Known name → mapped UUID, no log line."""
    known_id = uuid4()
    name_to_id = {"lunch": known_id}
    with caplog.at_level("WARNING", logger="openmarquee.schedule"):
        result = _resolve_legacy_name("lunch", name_to_id, context="rule 'x'")
    assert result == known_id
    assert caplog.records == []


def test_resolve_legacy_name_returns_default_for_empty_name(caplog):
    """Empty / None name → DEFAULT, no log line (the legacy "no
    playlist set" shape; expected at migration time, shouldn't spam
    operator logs)."""
    with caplog.at_level("WARNING", logger="openmarquee.schedule"):
        assert _resolve_legacy_name(None, {}, context="rule 'x'") == DEFAULT_PLAYLIST_ID
        assert _resolve_legacy_name("", {}, context="rule 'x'") == DEFAULT_PLAYLIST_ID
    assert caplog.records == []


def test_resolve_legacy_name_logs_and_falls_back_for_unknown_name(caplog):
    """Unknown name → DEFAULT + WARNING log carrying context + name.

    Locks the operator-facing message shape (`context` + `name`
    appear) without depending on the exact prose, so future
    rewording doesn't break this test but a context/name omission
    would.
    """
    name_to_id = {"lunch": uuid4()}
    with caplog.at_level("WARNING", logger="openmarquee.schedule"):
        result = _resolve_legacy_name("ghost-playlist", name_to_id, context="rule 'morning'")
    assert result == DEFAULT_PLAYLIST_ID
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "rule 'morning'" in msg
    assert "ghost-playlist" in msg


# --- Edge cases ---


def test_overnight_window_exclusive_at_end_boundary():
    rule = _rule(["fri"], "22:00", "02:00")
    # Saturday 02:00:00 — exclusive of end, should NOT match.
    assert rule.matches(datetime(2026, 4, 25, 2, 0)) is False


def test_start_equals_end_is_empty_window():
    rule = _rule(["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "10:00", "10:00")
    assert rule.matches(datetime(2026, 4, 21, 10, 0)) is False
    assert rule.matches(datetime(2026, 4, 21, 9, 59)) is False
    assert rule.matches(datetime(2026, 4, 21, 23, 59)) is False


def test_24_00_means_end_of_day():
    """All-day idiom: 00:00 to 24:00 covers the whole day including 23:59."""
    rule = _rule(["sat", "sun"], "00:00", "24:00", playlist_id=PL_WEEKEND)
    assert rule.matches(datetime(2026, 4, 25, 0, 0)) is True
    assert rule.matches(datetime(2026, 4, 25, 12, 0)) is True
    assert rule.matches(datetime(2026, 4, 25, 23, 59)) is True
    # Wrong day still fails.
    assert rule.matches(datetime(2026, 4, 24, 12, 0)) is False  # Friday


def test_disabled_rule_never_matches():
    """`enabled=False` lets users park a rule without deleting it."""
    rule = ScheduleRule(
        name="paused",
        days=["mon", "tue"],
        start_time="08:00",
        end_time="17:00",
        playlist_id=PL_WORKDAY,
        enabled=False,
    )
    assert rule.matches(datetime(2026, 4, 21, 12, 0)) is False


def test_schema_version_present_on_save(tmp_path: Path):
    storage = ScheduleStorage(tmp_path / "schedules.json")
    storage.save(Schedule())
    raw = json.loads((tmp_path / "schedules.json").read_text())
    assert raw["schema_version"] == SCHEDULE_SCHEMA_VERSION


def test_tz_field_defaults_to_none():
    schedule = Schedule()
    assert schedule.tz is None


def test_tz_field_round_trips_via_storage(tmp_path: Path):
    storage = ScheduleStorage(tmp_path / "schedules.json")
    storage.save(Schedule(tz="America/Los_Angeles"))
    assert storage.load().tz == "America/Los_Angeles"


def test_tz_field_length_capped():
    with pytest.raises(ValidationError):
        Schedule(tz="x" * 65)


def test_evaluator_ignores_tz_for_now():
    """The tz field is reserved — the evaluator still treats `now` as
    device-local. Pin this so a future zoned implementation is an obvious
    test-suite change."""
    schedule = Schedule(
        rules=[_rule(["tue"], "12:00", "13:00", playlist_id=PL_LUNCH)],
        default_playlist_id=DEFAULT_PLAYLIST_ID,
        tz="America/Los_Angeles",
    )
    # The rule fires at the *device's* 12:30, regardless of tz value.
    assert evaluate_schedule(datetime(2026, 4, 21, 12, 30), schedule) == PL_LUNCH
