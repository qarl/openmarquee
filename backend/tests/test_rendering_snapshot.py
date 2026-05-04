"""Tests for openmarquee.rendering.snapshot.SlideSnapshotCache."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from openmarquee.content import TextBox, TextLayer, TextSlide
from openmarquee.rendering.snapshot import SlideSnapshotCache


def _slide(*, auto: bool = False, name: str = "test") -> TextSlide:
    layer_kwargs = {
        "text": "hello",
        "name": name,
        "font_size_pct": 12.0,
        "text_color": "#ffffff",
        "box": TextBox(x=0.1, y=0.1, w=0.8, h=0.8),
    }
    if auto:
        layer_kwargs["auto_mode"] = "time"
    return TextSlide(
        id=uuid4(),
        name=name,
        background_color="#000000",
        text_layers=[TextLayer(**layer_kwargs)],
    )


def test_cache_hit_skips_compose_on_second_call():
    cache = SlideSnapshotCache()
    slide = _slide()
    with patch(
        "openmarquee.rendering.snapshot.compose_slide_rgba",
        return_value=b"x" * (4 * 4 * 4),
    ) as mock_compose:
        first = cache.get_full(slide, 4, 4)
        second = cache.get_full(slide, 4, 4)
    assert first == second == b"x" * 64
    assert mock_compose.call_count == 1, "second call should hit cache"


def test_cache_separates_full_from_bg_statics():
    cache = SlideSnapshotCache()
    slide = _slide()
    with patch(
        "openmarquee.rendering.snapshot.compose_slide_rgba",
        return_value=b"f" * 64,
    ) as mock_full, patch(
        "openmarquee.rendering.snapshot.compose_slide_bg_statics_rgba",
        return_value=b"b" * 64,
    ) as mock_bg:
        full = cache.get_full(slide, 4, 4)
        bg = cache.get_bg_statics(slide, 4, 4)
    assert full == b"f" * 64
    assert bg == b"b" * 64
    assert mock_full.call_count == 1
    assert mock_bg.call_count == 1


def test_cache_invalidates_on_updated_at_change():
    cache = SlideSnapshotCache()
    slide_v1 = _slide()
    # Pretend the slide updated_at advances; same id, new timestamp.
    slide_v2 = slide_v1.model_copy(
        update={"updated_at": (slide_v1.updated_at or datetime.now()) + timedelta(seconds=1)}
    )
    with patch(
        "openmarquee.rendering.snapshot.compose_slide_rgba",
        side_effect=[b"v1" + b"\x00" * 62, b"v2" + b"\x00" * 62],
    ) as mock_compose:
        v1 = cache.get_full(slide_v1, 4, 4)
        v2 = cache.get_full(slide_v2, 4, 4)
    assert v1.startswith(b"v1")
    assert v2.startswith(b"v2")
    assert mock_compose.call_count == 2, "stale entry must re-compose"


def test_auto_mode_slide_skips_cache():
    cache = SlideSnapshotCache()
    slide = _slide(auto=True)
    with patch(
        "openmarquee.rendering.snapshot.compose_slide_rgba",
        return_value=b"x" * 64,
    ) as mock_compose:
        cache.get_full(slide, 4, 4)
        cache.get_full(slide, 4, 4)
    assert mock_compose.call_count == 2, "auto-mode slide must always compose fresh"
    assert len(cache) == 0, "auto-mode slide must not populate cache"


def test_clear_drops_all_entries():
    cache = SlideSnapshotCache()
    slide = _slide()
    with patch(
        "openmarquee.rendering.snapshot.compose_slide_rgba",
        return_value=b"x" * 64,
    ):
        cache.get_full(slide, 4, 4)
    assert len(cache) == 1
    cache.clear()
    assert len(cache) == 0


def test_full_and_bg_statics_share_entry_per_slide():
    cache = SlideSnapshotCache()
    slide = _slide()
    with patch(
        "openmarquee.rendering.snapshot.compose_slide_rgba",
        return_value=b"f" * 64,
    ), patch(
        "openmarquee.rendering.snapshot.compose_slide_bg_statics_rgba",
        return_value=b"b" * 64,
    ):
        cache.get_full(slide, 4, 4)
        cache.get_bg_statics(slide, 4, 4)
    # One entry holding both variants.
    assert len(cache) == 1
