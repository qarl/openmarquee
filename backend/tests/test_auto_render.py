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
        "text",
        "text_color",
        "font_family",
        "font_size_px",
        "font_size_pct",
        "auto_mode",
        "auto_format",
        "box",
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


# --- Batch 8.6: image-bg LRU ---


def test_load_background_lru_skips_png_decode_on_cache_hit():
    """First load_background for a (slide_id, w, h) decodes; second
    returns the cached Image without a fresh decode. Sweep #2 #15."""
    from io import BytesIO
    from uuid import uuid4

    from PIL import Image as _Image

    from openmarquee.auto_render import (
        _stats,
        clear_image_bg_cache,
        load_background,
    )

    clear_image_bg_cache()
    for k in _stats:
        _stats[k] = 0

    buf = BytesIO()
    _Image.new("RGB", (16, 16), (10, 20, 30)).save(buf, format="PNG")
    png_bytes = buf.getvalue()
    bg_id = uuid4()

    def fake_read(_id):
        return png_bytes

    slide = _auto_slide(
        auto_mode="time",
        auto_format="time_hm",
        background_image_slide_id=bg_id,
    )
    load_background(slide, 16, 16, fake_read)
    assert _stats["png_decodes"] == 1
    assert _stats["image_bg_cache_hits"] == 0

    load_background(slide, 16, 16, fake_read)
    load_background(slide, 16, 16, fake_read)
    assert _stats["png_decodes"] == 1  # no new decodes
    assert _stats["image_bg_cache_hits"] == 2


def test_load_background_lru_evicts_at_cap():
    """The LRU caps at _IMAGE_BG_LRU_MAX entries (Batch 8.fix: 4);
    the (cap+1)th add evicts the oldest."""
    from io import BytesIO
    from uuid import uuid4

    from PIL import Image as _Image

    from openmarquee.auto_render import (
        _IMAGE_BG_LRU_MAX,
        _image_bg_cache,
        _stats,
        clear_image_bg_cache,
        load_background,
    )

    clear_image_bg_cache()
    for k in _stats:
        _stats[k] = 0

    buf = BytesIO()
    _Image.new("RGB", (8, 8), (0, 0, 0)).save(buf, format="PNG")
    png_bytes = buf.getvalue()

    def fake_read(_id):
        return png_bytes

    ids = [uuid4() for _ in range(_IMAGE_BG_LRU_MAX + 1)]
    for i in ids:
        slide = _auto_slide(
            auto_mode="time",
            auto_format="time_hm",
            background_image_slide_id=i,
        )
        load_background(slide, 8, 8, fake_read)

    assert len(_image_bg_cache) == _IMAGE_BG_LRU_MAX
    # Oldest evicted; newest present.
    assert (ids[0], 8, 8) not in _image_bg_cache
    assert (ids[-1], 8, 8) in _image_bg_cache


# --- Sweep #3 #4: render_auto_text_for_layer unknown-mode fall-through ---


def test_render_auto_text_for_layer_unknown_mode_returns_typed_text():
    """If a future schema mode lands ahead of the helper (e.g. a new
    auto_mode="weather" appears in saved slides before the renderer
    is updated), the fall-through should return the layer's typed
    `text` rather than crash. model_construct bypasses schema
    validation so we can test the defensive shape."""
    from openmarquee.auto_render import render_auto_text_for_layer
    from openmarquee.content import TextLayer

    layer = TextLayer.model_construct(
        text="fallback text",
        auto_mode="future_weather_mode",
        auto_format=None,
    )
    now = datetime(2026, 5, 10, 14, 30, tzinfo=ZoneInfo("UTC"))
    assert render_auto_text_for_layer(layer, now) == "fallback text"


# render_pattern + _dot_grid_mask deleted in DELETE-PIL phase 3b.
# Pattern rasterization moved to Canvas2D + bg-system.js (parity
# harness covers visual correctness now). Their tests are removed.
