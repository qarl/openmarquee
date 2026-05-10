"""Unit tests for openmarquee.auto_render — the playback-time render-over
path for auto-mode text slides.

These are pure-function tests: we pass a fixed `now` in so assertions
don't depend on the test host's clock or timezone.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from openmarquee.auto_render import (
    render_auto_text,
    resolve_timezone,
)
from openmarquee.content import TextLayer, TextSlide


def _auto_slide(**kwargs) -> TextSlide:
    """Helper: builds a single-layer TextSlide for the auto-render tests.
    Schema v3 routed text fields off the slide root; accept the flat
    kwargs the tests already use and route them into text_layers[0]."""
    layer_keys = {
        "text", "text_color", "font_family", "font_size_px",
        "font_size_pct", "auto_mode", "auto_format", "box",
    }
    layer = {"text": kwargs.pop("text", "placeholder")}
    for k in list(kwargs.keys()):
        if k in layer_keys:
            layer[k] = kwargs.pop(k)
    return TextSlide(
        name=kwargs.pop("name", "auto"),
        text_layers=[TextLayer(**layer)],
        **kwargs,
    )


# --- render_auto_text: string formatting per mode + format ---


class TestRenderAutoText:
    def test_non_auto_slide_returns_typed_text(self):
        slide = _auto_slide(text="hello")
        now = datetime(2026, 4, 21, 14, 30, 45, tzinfo=ZoneInfo("UTC"))
        assert render_auto_text(slide, now) == "hello"

    def test_time_hm_default(self):
        slide = _auto_slide(auto_mode="time")
        now = datetime(2026, 4, 21, 14, 30, 45, tzinfo=ZoneInfo("UTC"))
        assert render_auto_text(slide, now) == "14:30"

    def test_time_hms(self):
        slide = _auto_slide(auto_mode="time", auto_format="time_hms")
        now = datetime(2026, 4, 21, 14, 30, 45, tzinfo=ZoneInfo("UTC"))
        assert render_auto_text(slide, now) == "14:30:45"

    def test_date_iso(self):
        slide = _auto_slide(auto_mode="date", auto_format="date_iso")
        now = datetime(2026, 4, 21, tzinfo=ZoneInfo("UTC"))
        assert render_auto_text(slide, now) == "2026-04-21"

    def test_date_long_default_drops_leading_zero(self):
        slide = _auto_slide(auto_mode="date")  # default date_iso — override to long
        slide = _auto_slide(auto_mode="date", auto_format="date_long")
        now = datetime(2026, 4, 7, tzinfo=ZoneInfo("UTC"))
        # Day has no leading zero: "April 7, 2026", not "April 07, 2026".
        assert render_auto_text(slide, now) == "April 7, 2026"

    def test_date_medium(self):
        slide = _auto_slide(auto_mode="date", auto_format="date_medium")
        now = datetime(2026, 4, 7, tzinfo=ZoneInfo("UTC"))
        # "Apr 7" — no zero-pad, portable (no %-d).
        assert render_auto_text(slide, now) == "Apr 7"

    def test_day_long(self):
        slide = _auto_slide(auto_mode="day", auto_format="day_long")
        # 2026-04-21 is a Tuesday
        now = datetime(2026, 4, 21, tzinfo=ZoneInfo("UTC"))
        assert render_auto_text(slide, now) == "Tuesday"

    def test_day_short(self):
        slide = _auto_slide(auto_mode="day", auto_format="day_short")
        now = datetime(2026, 4, 21, tzinfo=ZoneInfo("UTC"))
        assert render_auto_text(slide, now) == "Tue"


# --- cross-field validator on the model ---


class TestAutoFormatValidator:
    def test_accepts_matching_mode_and_format(self):
        s = _auto_slide(auto_mode="time", auto_format="time_hms")
        assert s.text_layers[0].auto_format == "time_hms"

    def test_rejects_format_without_mode(self):
        with pytest.raises(Exception):  # ValidationError — let pydantic match
            _auto_slide(auto_format="time_hm")

    def test_rejects_mode_mismatch(self):
        with pytest.raises(Exception):
            _auto_slide(auto_mode="time", auto_format="day_long")

    def test_mode_without_format_is_allowed(self):
        s = _auto_slide(auto_mode="time")
        assert s.text_layers[0].auto_format is None


# --- timezone resolution ---


class TestResolveTimezone:
    def test_known_tz(self):
        tz = resolve_timezone("America/Los_Angeles")
        assert tz == ZoneInfo("America/Los_Angeles")

    def test_none_falls_back_to_utc(self):
        assert resolve_timezone(None) == ZoneInfo("UTC")

    def test_empty_falls_back_to_utc(self):
        assert resolve_timezone("") == ZoneInfo("UTC")

    def test_unknown_falls_back_to_utc(self):
        # "Mars/Olympus_Mons" isn't in the tzdata — should log + return UTC.
        assert resolve_timezone("Mars/Olympus_Mons") == ZoneInfo("UTC")


# compose_auto_frame was deleted in Batch 5.3 -- the unified
# motion.compose_motion_frame is the only render path; equivalent
# coverage lives in test_motion.py (auto-layer outline test at line
# 602, dim/bg/timing tests in TestComposeMotionFrame).
