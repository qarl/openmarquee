from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from openmarquee.schedule import (
    Schedule,
    ScheduleRule,
    ScheduleStorage,
    evaluate_schedule,
)

# --- ScheduleRule field validation ---


def test_rule_requires_at_least_one_day():
    with pytest.raises(ValidationError):
        ScheduleRule(name="x", days=[], start_time="08:00", end_time="17:00", playlist_name="p")


def test_rule_rejects_unknown_day():
    with pytest.raises(ValidationError):
        ScheduleRule(
            name="x",
            days=["funday"],  # type: ignore[list-item]
            start_time="08:00",
            end_time="17:00",
            playlist_name="p",
        )


def test_rule_rejects_malformed_time():
    with pytest.raises(ValidationError):
        ScheduleRule(name="x", days=["mon"], start_time="8:00", end_time="17:00", playlist_name="p")


def test_rule_rejects_out_of_range_time():
    with pytest.raises(ValidationError):
        ScheduleRule(
            name="x", days=["mon"], start_time="24:00", end_time="25:00", playlist_name="p"
        )


# --- ScheduleRule.matches ---


def _rule(days, start, end, name="r", playlist="p"):
    return ScheduleRule(
        name=name, days=days, start_time=start, end_time=end, playlist_name=playlist
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
    schedule = Schedule(default_playlist_name="default")
    assert evaluate_schedule(datetime(2026, 4, 21, 12, 0), schedule) == "default"


def test_first_matching_rule_wins():
    schedule = Schedule(
        rules=[
            _rule(["mon", "tue"], "11:00", "14:00", playlist="lunch"),
            _rule(["mon", "tue"], "08:00", "17:00", playlist="workday"),
        ],
        default_playlist_name="default",
    )
    # Tuesday 12:00 matches both — the lunch rule wins because it's first.
    assert evaluate_schedule(datetime(2026, 4, 21, 12, 0), schedule) == "lunch"
    # Tuesday 09:00 only matches workday.
    assert evaluate_schedule(datetime(2026, 4, 21, 9, 0), schedule) == "workday"


def test_no_matching_rule_falls_back_to_default():
    schedule = Schedule(
        rules=[_rule(["mon"], "08:00", "17:00", playlist="weekday")],
        default_playlist_name="default",
    )
    # Tuesday — wrong day for the only rule.
    assert evaluate_schedule(datetime(2026, 4, 21, 12, 0), schedule) == "default"


# --- ScheduleStorage ---


def test_empty_load_returns_empty_schedule(tmp_path: Path):
    storage = ScheduleStorage(tmp_path / "schedules.json")
    schedule = storage.load()
    assert schedule.rules == []
    assert schedule.default_playlist_name == "default"


def test_save_then_load_round_trips(tmp_path: Path):
    storage = ScheduleStorage(tmp_path / "schedules.json")
    rule = _rule(["sat", "sun"], "00:00", "23:59", name="weekend", playlist="weekend_promo")
    schedule = Schedule(rules=[rule], default_playlist_name="quiet")
    storage.save(schedule)

    loaded = storage.load()
    assert loaded.default_playlist_name == "quiet"
    assert len(loaded.rules) == 1
    assert loaded.rules[0].playlist_name == "weekend_promo"


def test_atomic_write_leaves_no_tmp_files(tmp_path: Path):
    storage = ScheduleStorage(tmp_path / "schedules.json")
    storage.save(Schedule())
    assert list(tmp_path.glob("*.tmp")) == []


# --- Edge cases the reviewer flagged ---


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
    rule = _rule(["sat", "sun"], "00:00", "24:00", playlist="weekend")
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
        playlist_name="workday",
        enabled=False,
    )
    assert rule.matches(datetime(2026, 4, 21, 12, 0)) is False


def test_playlist_name_validates_format():
    with pytest.raises(ValidationError):
        ScheduleRule(
            name="x",
            days=["mon"],
            start_time="08:00",
            end_time="17:00",
            playlist_name="",
        )
    with pytest.raises(ValidationError):
        ScheduleRule(
            name="x",
            days=["mon"],
            start_time="08:00",
            end_time="17:00",
            playlist_name="Has Spaces",
        )
    with pytest.raises(ValidationError):
        ScheduleRule(
            name="x",
            days=["mon"],
            start_time="08:00",
            end_time="17:00",
            playlist_name="x" * 65,
        )


def test_default_playlist_name_validates_format():
    with pytest.raises(ValidationError):
        Schedule(default_playlist_name="")


def test_schema_version_present_on_save(tmp_path: Path):
    import json

    storage = ScheduleStorage(tmp_path / "schedules.json")
    storage.save(Schedule())
    raw = json.loads((tmp_path / "schedules.json").read_text())
    assert raw["schema_version"] == 1