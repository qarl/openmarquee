import asyncio
import io
from datetime import datetime
from uuid import UUID

import pytest
from PIL import Image

from openmarquee.content import ImageSlide, TextSlide
from openmarquee.playback import PlaybackLoop
from openmarquee.rendering.mock import MockRenderer

# 100ms is the model's minimum duration. Tests use it directly; the
# total runtime stays under a second.
_FAST_DURATION_MS = 100
_FAST_EMPTY_POLL = 0.01  # so loops with no items spin quickly in tests


def _new_loop(
    renderer,
    *,
    fetch_items,
    read_asset,
    get_timezone=None,
    auto_tick_seconds=0.02,
):
    return PlaybackLoop(
        renderer,
        fetch_items=fetch_items,
        read_asset=read_asset,
        empty_playlist_poll_seconds=_FAST_EMPTY_POLL,
        get_timezone=get_timezone,
        auto_tick_seconds=auto_tick_seconds,
    )


def _png_bytes(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_slide(name: str, color: tuple[int, int, int]) -> tuple[TextSlide, bytes]:
    slide = TextSlide(name=name, text=name, duration_ms=_FAST_DURATION_MS)
    return slide, _png_bytes(8, 8, color)


def _renderer(tmp_path) -> MockRenderer:
    return MockRenderer(8, 8, tmp_path / "out.png")


@pytest.fixture
def renderer(tmp_path):
    return _renderer(tmp_path)


@pytest.mark.asyncio
async def test_start_and_stop_toggles_is_running(renderer):
    loop = _new_loop(renderer, fetch_items=lambda: [], read_asset=lambda _id: b"")
    assert loop.is_running is False
    await loop.start()
    assert loop.is_running is True
    await loop.stop()
    assert loop.is_running is False


@pytest.mark.asyncio
async def test_double_start_is_a_no_op(renderer):
    loop = _new_loop(renderer, fetch_items=lambda: [], read_asset=lambda _id: b"")
    await loop.start()
    task_1 = loop._task
    await loop.start()
    assert loop._task is task_1  # same task, not replaced
    await loop.stop()


@pytest.mark.asyncio
async def test_empty_playlist_does_not_render_or_crash(renderer):
    loop = _new_loop(renderer, fetch_items=lambda: [], read_asset=lambda _id: b"")
    await loop.start()
    await asyncio.sleep(0.05)  # several empty-poll iterations
    assert renderer.last_frame is None
    assert loop.current_item_id is None
    await loop.stop()


@pytest.mark.asyncio
async def test_single_item_renders_to_renderer(renderer):
    slide, png = _make_slide("a", (255, 0, 0))
    loop = _new_loop(renderer, fetch_items=lambda: [slide], read_asset=lambda _id: png)
    await loop.start()
    await asyncio.sleep(0.05)  # less than the slide's 100ms duration
    expected = bytes((255, 0, 0)) * (renderer.width * renderer.height)
    assert renderer.last_frame == expected
    assert loop.current_item_id == slide.id
    await loop.stop()


@pytest.mark.asyncio
async def test_cycles_through_multiple_items_in_order(renderer):
    slide_a, png_a = _make_slide("a", (255, 0, 0))
    slide_b, png_b = _make_slide("b", (0, 255, 0))
    items = [slide_a, slide_b]
    assets = {slide_a.id: png_a, slide_b.id: png_b}

    rendered_ids: list[UUID] = []
    original_render = renderer.render_frame

    def track(frame: bytes) -> None:
        rendered_ids.append(loop.current_item_id)
        original_render(frame)

    renderer.render_frame = track  # type: ignore[method-assign]

    loop = _new_loop(
        renderer,
        fetch_items=lambda: items,
        read_asset=lambda item_id: assets[item_id],
    )
    await loop.start()
    # 100ms per slide × 2 slides + a touch of slack for the loop to advance.
    await asyncio.sleep(0.25)
    await loop.stop()

    # We should have rendered each slide at least once, in order a, b.
    assert rendered_ids[0] == slide_a.id
    assert slide_b.id in rendered_ids


@pytest.mark.asyncio
async def test_missing_asset_is_logged_and_skipped(renderer):
    slide_ok, png_ok = _make_slide("ok", (0, 255, 0))
    slide_missing = TextSlide(name="missing", text="x", duration_ms=_FAST_DURATION_MS)

    def read_asset(item_id: UUID) -> bytes:
        if item_id == slide_missing.id:
            raise FileNotFoundError(item_id)
        return png_ok

    loop = _new_loop(
        renderer,
        fetch_items=lambda: [slide_missing, slide_ok],
        read_asset=read_asset,
    )
    await loop.start()
    await asyncio.sleep(0.25)
    await loop.stop()

    # Even though the first slide was missing, the second should have rendered.
    assert renderer.last_frame == bytes((0, 255, 0)) * (renderer.width * renderer.height)


@pytest.mark.asyncio
async def test_corrupt_asset_is_logged_and_skipped(renderer):
    slide_corrupt = TextSlide(name="corrupt", text="x", duration_ms=_FAST_DURATION_MS)
    slide_ok, png_ok = _make_slide("ok", (0, 0, 255))
    assets = {slide_corrupt.id: b"not a PNG", slide_ok.id: png_ok}

    loop = _new_loop(
        renderer,
        fetch_items=lambda: [slide_corrupt, slide_ok],
        read_asset=lambda item_id: assets[item_id],
    )
    await loop.start()
    await asyncio.sleep(0.25)
    await loop.stop()

    assert renderer.last_frame == bytes((0, 0, 255)) * (renderer.width * renderer.height)


@pytest.mark.asyncio
async def test_stop_returns_promptly_during_long_duration(renderer):
    long_slide = TextSlide(name="long", text="x", duration_ms=10_000)
    _, png = _make_slide("dummy", (255, 255, 255))

    loop = _new_loop(
        renderer,
        fetch_items=lambda: [long_slide],
        read_asset=lambda _id: png,
    )
    await loop.start()
    await asyncio.sleep(0.05)  # let the slide start rendering and enter the wait

    start = asyncio.get_event_loop().time()
    await loop.stop()
    elapsed = asyncio.get_event_loop().time() - start
    # Tight bound — stop should propagate within tens of ms, not seconds.
    # Anything looser hides real bugs.
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_items_change_between_iterations_takes_effect(renderer):
    items_box: list[TextSlide] = []
    assets: dict[UUID, bytes] = {}

    loop = _new_loop(
        renderer,
        fetch_items=lambda: list(items_box),
        read_asset=lambda item_id: assets[item_id],
    )
    await loop.start()
    await asyncio.sleep(0.03)  # several empty-poll iterations (10ms poll)
    assert renderer.last_frame is None

    slide, png = _make_slide("late", (255, 128, 0))
    items_box.append(slide)
    assets[slide.id] = png

    await asyncio.sleep(0.05)  # next iteration picks it up
    assert renderer.last_frame == bytes((255, 128, 0)) * (renderer.width * renderer.height)
    await loop.stop()


@pytest.mark.asyncio
async def test_fetch_items_raising_does_not_kill_the_loop(renderer):
    """An exception from fetch_items shouldn't terminate playback —
    the loop should treat the iteration as empty and try again."""
    calls = {"n": 0}

    def flaky_fetch():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("storage went away")
        # Eventually return an item.
        slide, png = _make_slide("recovered", (255, 255, 255))
        flaky_fetch.png = png  # keep reference for read_asset closure
        flaky_fetch.id = slide.id
        return [slide]

    loop = _new_loop(
        renderer,
        fetch_items=flaky_fetch,
        read_asset=lambda _id: flaky_fetch.png,
    )
    await loop.start()
    await asyncio.sleep(0.1)  # several poll iterations including failures
    await loop.stop()

    # The loop survived the failures and ultimately rendered.
    assert calls["n"] >= 3
    assert renderer.last_frame is not None


@pytest.mark.asyncio
async def test_renderer_raising_is_swallowed(renderer):
    """If render_frame itself raises, the loop should log and keep going."""
    slide_a, png_a = _make_slide("a", (10, 20, 30))
    slide_b, png_b = _make_slide("b", (40, 50, 60))
    assets = {slide_a.id: png_a, slide_b.id: png_b}

    fail_for = {slide_a.id}
    original_render = renderer.render_frame

    def explosive_render(frame: bytes) -> None:
        if loop.current_item_id in fail_for:
            raise RuntimeError("renderer fell over")
        original_render(frame)

    loop = _new_loop(
        renderer,
        fetch_items=lambda: [slide_a, slide_b],
        read_asset=lambda item_id: assets[item_id],
    )
    renderer.render_frame = explosive_render  # type: ignore[method-assign]

    await loop.start()
    await asyncio.sleep(0.25)
    await loop.stop()

    # Slide B rendered successfully despite slide A's renderer crash.
    assert renderer.last_frame == bytes((40, 50, 60)) * (renderer.width * renderer.height)


def _track_frames(renderer):
    """Wrap renderer.render_frame so the test can inspect every frame the loop
    pushes (not just the last one)."""
    rendered: list[bytes] = []
    original = renderer.render_frame

    def track(frame: bytes) -> None:
        rendered.append(frame)
        original(frame)

    renderer.render_frame = track  # type: ignore[method-assign]
    return rendered


@pytest.mark.asyncio
async def test_cut_transition_emits_no_intermediate_frames(renderer):
    """Default cut transition: between slide A's last frame and slide B's
    first, nothing else gets rendered."""
    slide_a, png_a = _make_slide("a", (255, 0, 0))
    slide_b, png_b = _make_slide("b", (0, 255, 0))
    items = [slide_a, slide_b]
    assets = {slide_a.id: png_a, slide_b.id: png_b}
    rendered = _track_frames(renderer)

    loop = _new_loop(renderer, fetch_items=lambda: items, read_asset=lambda i: assets[i])
    await loop.start()
    await asyncio.sleep(0.3)  # >= duration_a + duration_b
    await loop.stop()

    pure_red = bytes((255, 0, 0)) * (renderer.width * renderer.height)
    pure_green = bytes((0, 255, 0)) * (renderer.width * renderer.height)
    # Only solid red and solid green frames — no intermediates.
    for frame in rendered:
        assert frame in (pure_red, pure_green)


@pytest.mark.asyncio
async def test_fade_transition_emits_blended_frames(renderer):
    """Fade transition: between A and B we should see frames that aren't
    pure A or pure B (Image.blend producing intermediate alphas)."""
    slide_a, png_a = _make_slide("a", (255, 0, 0))
    slide_b, png_b = _make_slide("b", (0, 255, 0))
    slide_a = slide_a.model_copy(
        update={"transition": "fade", "transition_ms": 200, "duration_ms": 100}
    )
    slide_b = slide_b.model_copy(update={"duration_ms": 100})
    items = [slide_a, slide_b]
    assets = {slide_a.id: png_a, slide_b.id: png_b}
    rendered = _track_frames(renderer)

    loop = _new_loop(renderer, fetch_items=lambda: items, read_asset=lambda i: assets[i])
    await loop.start()
    # 100ms A + 200ms fade + 100ms B = 400ms; give some slack.
    await asyncio.sleep(0.6)
    await loop.stop()

    pure_red = bytes((255, 0, 0)) * (renderer.width * renderer.height)
    pure_green = bytes((0, 255, 0)) * (renderer.width * renderer.height)
    has_red = any(f == pure_red for f in rendered)
    has_green = any(f == pure_green for f in rendered)
    intermediates = [f for f in rendered if f != pure_red and f != pure_green]

    assert has_red, "expected solid-red frames from slide A"
    assert has_green, "expected solid-green frames from slide B"
    assert intermediates, "expected blended frames during the fade"


@pytest.mark.asyncio
async def test_fade_transition_wraps_from_last_to_first(renderer):
    """Regression: the LAST slide's transition must honor its setting —
    a fade on the last slide should fade into the first slide on wrap,
    not cut. qarl saw the wrap always do a cut."""
    slide_a, png_a = _make_slide("a", (255, 0, 0))
    slide_b, png_b = _make_slide("b", (0, 0, 255))
    # Both slides fade so we can observe the B→A wrap as well as A→B.
    slide_a = slide_a.model_copy(
        update={"transition": "fade", "transition_ms": 200, "duration_ms": 100}
    )
    slide_b = slide_b.model_copy(
        update={"transition": "fade", "transition_ms": 200, "duration_ms": 100}
    )
    items = [slide_a, slide_b]
    assets = {slide_a.id: png_a, slide_b.id: png_b}
    rendered = _track_frames(renderer)

    loop = _new_loop(renderer, fetch_items=lambda: items, read_asset=lambda i: assets[i])
    await loop.start()
    # A + fade + B + fade + A + fade + B ≈ 1200ms; give slack to catch
    # the B→A wrap fade.
    await asyncio.sleep(1.0)
    await loop.stop()

    pure_red = bytes((255, 0, 0)) * (renderer.width * renderer.height)
    pure_blue = bytes((0, 0, 255)) * (renderer.width * renderer.height)

    # Group consecutive intermediate-frame runs to detect fade-shaped
    # transitions. Each fade should produce a run of intermediates
    # sandwiched between pure-A and pure-B (or vice versa). If the
    # wrap was a hard cut, there'd be one or zero intermediate runs.
    runs = []
    cur = []
    for f in rendered:
        if f == pure_red or f == pure_blue:
            if cur:
                runs.append(cur)
                cur = []
        else:
            cur.append(f)
    if cur:
        runs.append(cur)

    # Expect at least TWO distinct fade runs within one cycle (A→B and B→A).
    assert len(runs) >= 2, (
        f"expected both A→B and B→A fades to produce intermediate frames; "
        f"got {len(runs)} run(s)"
    )


@pytest.mark.asyncio
async def test_single_item_playlist_with_fade_skips_fade(renderer):
    """Fade requires a different next slide. With one item, next == current
    so the fade is a waste — make sure we skip it instead of doing pointless work."""
    slide, png = _make_slide("solo", (123, 45, 67))
    slide = slide.model_copy(
        update={"transition": "fade", "transition_ms": 1000, "duration_ms": 100}
    )
    rendered = _track_frames(renderer)

    loop = _new_loop(renderer, fetch_items=lambda: [slide], read_asset=lambda _i: png)
    await loop.start()
    await asyncio.sleep(0.25)  # > 2x duration; would catch a wasteful fade
    await loop.stop()

    # Every frame is the solid color of the lone slide.
    expected = bytes((123, 45, 67)) * (renderer.width * renderer.height)
    for frame in rendered:
        assert frame == expected


@pytest.mark.asyncio
async def test_stop_during_fade_returns_promptly(renderer):
    """Stop request should propagate within tens of ms even mid-fade."""
    slide_a, png_a = _make_slide("a", (255, 0, 0))
    slide_b, png_b = _make_slide("b", (0, 255, 0))
    # Long fade so we definitely catch the loop inside it.
    slide_a = slide_a.model_copy(
        update={"transition": "fade", "transition_ms": 5000, "duration_ms": 100}
    )
    items = [slide_a, slide_b]
    assets = {slide_a.id: png_a, slide_b.id: png_b}

    loop = _new_loop(renderer, fetch_items=lambda: items, read_asset=lambda i: assets[i])
    await loop.start()
    # Wait long enough to enter the fade, then stop.
    await asyncio.sleep(0.15)

    start = asyncio.get_event_loop().time()
    await loop.stop()
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.2  # well under the 5s fade


@pytest.mark.asyncio
async def test_scroll_transition_emits_split_frames(renderer):
    """Scroll transition: vertical roll — at progress p, the top
    (h - p*h) rows of the frame are from_image and the bottom p*h rows
    are to_image. So a mid-transition frame has BOTH source colors
    visible at distinct rows, distinguishing scroll from fade (which
    blends per-pixel and never preserves either pure color mid-way)."""
    slide_a, png_a = _make_slide("a", (255, 0, 0))
    slide_b, png_b = _make_slide("b", (0, 0, 255))
    slide_a = slide_a.model_copy(
        update={"transition": "scroll", "transition_ms": 300, "duration_ms": 100}
    )
    slide_b = slide_b.model_copy(update={"duration_ms": 100})
    items = [slide_a, slide_b]
    assets = {slide_a.id: png_a, slide_b.id: png_b}
    rendered = _track_frames(renderer)

    loop = _new_loop(renderer, fetch_items=lambda: items, read_asset=lambda i: assets[i])
    await loop.start()
    await asyncio.sleep(0.7)
    await loop.stop()

    # An 8×8 RGB frame is 192 bytes; pixel i lives at offset i*3.
    width, height = renderer.width, renderer.height
    pure_red = bytes((255, 0, 0)) * (width * height)
    pure_blue = bytes((0, 0, 255)) * (width * height)

    def has_split(frame: bytes) -> bool:
        # Look for at least one fully-red row AND at least one fully-blue
        # row in the same frame. That's what scroll produces and what
        # fade/cut never can.
        red_row = bytes((255, 0, 0)) * width
        blue_row = bytes((0, 0, 255)) * width
        rows = [frame[y * width * 3 : (y + 1) * width * 3] for y in range(height)]
        return red_row in rows and blue_row in rows

    assert any(f == pure_red for f in rendered), "expected pure-red frames from A"
    assert any(f == pure_blue for f in rendered), "expected pure-blue frames from B"
    assert any(has_split(f) for f in rendered), (
        "expected at least one split frame with both pure-red and pure-blue rows"
    )


@pytest.mark.asyncio
async def test_flip_transition_emits_squished_frames(renderer):
    """Flip transition: from-image scaleX-shrinks to a center column,
    then to-image scaleX-grows from a center column. Mid-transition
    we should see frames where most of the canvas is BLACK (the cleared
    PIL Image.new background) with only a center band of color — that's
    the distinguishing card-flip silhouette no other transition emits."""
    slide_a, png_a = _make_slide("a", (255, 0, 0))
    slide_b, png_b = _make_slide("b", (0, 0, 255))
    slide_a = slide_a.model_copy(
        update={"transition": "flip", "transition_ms": 300, "duration_ms": 100}
    )
    slide_b = slide_b.model_copy(update={"duration_ms": 100})
    items = [slide_a, slide_b]
    assets = {slide_a.id: png_a, slide_b.id: png_b}
    rendered = _track_frames(renderer)

    loop = _new_loop(renderer, fetch_items=lambda: items, read_asset=lambda i: assets[i])
    await loop.start()
    await asyncio.sleep(0.7)
    await loop.stop()

    width, height = renderer.width, renderer.height
    pure_red = bytes((255, 0, 0)) * (width * height)
    pure_blue = bytes((0, 0, 255)) * (width * height)
    black_pixel = bytes((0, 0, 0))

    def has_black_edges_with_center_color(frame: bytes) -> bool:
        # Mid-flip: leftmost and rightmost columns should be black
        # (the un-pasted PIL canvas), with at least one center-column
        # pixel still red or blue.
        # Pixel at (x, y) lives at offset (y * width + x) * 3.
        left_col_black = all(
            frame[(y * width) * 3 : (y * width) * 3 + 3] == black_pixel
            for y in range(height)
        )
        right_col_black = all(
            frame[(y * width + width - 1) * 3 : (y * width + width - 1) * 3 + 3]
            == black_pixel
            for y in range(height)
        )
        center_x = width // 2
        center_pixel = frame[
            (0 * width + center_x) * 3 : (0 * width + center_x) * 3 + 3
        ]
        return left_col_black and right_col_black and center_pixel != black_pixel

    assert any(f == pure_red for f in rendered), "expected pure-red frames from A"
    assert any(f == pure_blue for f in rendered), "expected pure-blue frames from B"
    assert any(has_black_edges_with_center_color(f) for f in rendered), (
        "expected at least one mid-flip frame with black edges + center color"
    )


@pytest.mark.asyncio
async def test_flip_transition_falls_back_to_fade_on_narrow_strip(tmp_path):
    """Strip-graceful: a horizontal scaleX flip on a width=1 panel has
    no visible motion, so _flip delegates to _fade. Verify by rendering
    on a 1×8 mock renderer and asserting we see blended (intermediate)
    frames — fade emits those, raw flip on width=1 would not."""
    from openmarquee.rendering.mock import MockRenderer

    # 1-wide strip — the WS281x columnar case the spec calls out.
    strip_renderer = MockRenderer(1, 8, tmp_path / "strip.png")
    slide_a, _ = _make_slide("a", (255, 0, 0))
    slide_b, _ = _make_slide("b", (0, 0, 255))
    slide_a = slide_a.model_copy(
        update={"transition": "flip", "transition_ms": 200, "duration_ms": 100}
    )
    slide_b = slide_b.model_copy(update={"duration_ms": 100})
    # PNGs are sized to the 8x8 helper default — _safe_load_image will
    # _cover_fit them to 1x8.
    png_a = _png_bytes(8, 8, (255, 0, 0))
    png_b = _png_bytes(8, 8, (0, 0, 255))
    items = [slide_a, slide_b]
    assets = {slide_a.id: png_a, slide_b.id: png_b}
    rendered = _track_frames(strip_renderer)

    loop = _new_loop(
        strip_renderer, fetch_items=lambda: items, read_asset=lambda i: assets[i]
    )
    await loop.start()
    await asyncio.sleep(0.5)
    await loop.stop()

    pure_red_strip = bytes((255, 0, 0)) * 8  # 1×8 = 8 pixels
    pure_blue_strip = bytes((0, 0, 255)) * 8
    intermediates = [f for f in rendered if f != pure_red_strip and f != pure_blue_strip]
    # Fade emits per-pixel-blended frames; raw flip on width=1 emits
    # only the source columns (no blending), so any intermediate
    # confirms the fallback happened.
    assert intermediates, "expected fade-shaped blended frames on the strip fallback"


@pytest.mark.asyncio
async def test_image_slide_renders_identically_to_text_slide(renderer):
    """The playback engine is type-agnostic — both variants store PNGs and go
    through the same decode → render path. Pin the behavior so future variants
    don't accidentally break it."""
    image = ImageSlide(name="logo", duration_ms=_FAST_DURATION_MS)
    png = _png_bytes(8, 8, (42, 42, 42))

    loop = _new_loop(renderer, fetch_items=lambda: [image], read_asset=lambda _id: png)
    await loop.start()
    await asyncio.sleep(0.05)
    expected = bytes((42, 42, 42)) * (renderer.width * renderer.height)
    assert renderer.last_frame == expected
    assert loop.current_item_id == image.id
    await loop.stop()


# --- scheduled_fetch_items (multi-playlist + schedule wiring) ---


def test_scheduled_fetch_uses_default_when_schedule_is_empty(tmp_path):
    """No schedule rules → falls back to default_playlist_id."""
    import io as _io
    from uuid import uuid4

    from PIL import Image as _Image

    from openmarquee.content.storage import ContentStorage
    from openmarquee.playback import scheduled_fetch_items
    from openmarquee.playlist import (
        DEFAULT_PLAYLIST_ID,
        Playlist,
        PlaylistStorage,
    )
    from openmarquee.schedule import ScheduleStorage

    storage = ContentStorage(tmp_path / "content")
    playlist_storage = PlaylistStorage(tmp_path / "playlists.json")
    schedule_storage = ScheduleStorage(tmp_path / "schedules.json")

    # Save one item to default + a different one to a separate playlist.
    text_in_default = TextSlide(name="in default", text="x")
    text_in_lunch = TextSlide(name="in lunch", text="x")

    def _png():
        img = _Image.new("RGB", (4, 4), (0, 0, 0))
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    storage.save_text_slide(text_in_default, _png())
    storage.save_text_slide(text_in_lunch, _png())
    # Default playlist (well-known id) carries the default item.
    default_pl = Playlist(id=DEFAULT_PLAYLIST_ID, name="default")
    default_pl.append(text_in_default.id)
    playlist_storage.set_by_id(default_pl)
    # Lunch playlist gets a fresh id.
    lunch_pl = Playlist(name="lunch")
    lunch_pl.append(text_in_lunch.id)
    playlist_storage.set_by_id(lunch_pl)

    # Empty schedule → default → returns the default-playlist item.
    items = scheduled_fetch_items(
        storage, playlist_storage, schedule_storage, datetime(2026, 4, 21, 12, 0)
    )
    assert [item.id for item in items] == [text_in_default.id]


def test_scheduled_fetch_picks_active_playlist_per_schedule(tmp_path):
    """Schedule with a rule that matches `now` → returns that playlist's items."""
    import io as _io

    from PIL import Image as _Image

    from openmarquee.content.storage import ContentStorage
    from openmarquee.playback import scheduled_fetch_items
    from openmarquee.playlist import (
        DEFAULT_PLAYLIST_ID,
        Playlist,
        PlaylistStorage,
    )
    from openmarquee.schedule import (
        Schedule,
        ScheduleRule,
        ScheduleStorage,
    )

    storage = ContentStorage(tmp_path / "content")
    playlist_storage = PlaylistStorage(tmp_path / "playlists.json")
    schedule_storage = ScheduleStorage(tmp_path / "schedules.json")

    text_default = TextSlide(name="default", text="x")
    text_lunch = TextSlide(name="lunch", text="x")

    def _png():
        img = _Image.new("RGB", (4, 4), (0, 0, 0))
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    storage.save_text_slide(text_default, _png())
    storage.save_text_slide(text_lunch, _png())
    default_pl = Playlist(id=DEFAULT_PLAYLIST_ID, name="default")
    default_pl.append(text_default.id)
    playlist_storage.set_by_id(default_pl)
    lunch_pl = Playlist(name="lunch")
    lunch_pl.append(text_lunch.id)
    playlist_storage.set_by_id(lunch_pl)

    schedule_storage.save(
        Schedule(
            rules=[
                ScheduleRule(
                    name="lunchtime",
                    days=["mon", "tue", "wed", "thu", "fri"],
                    start_time="11:00",
                    end_time="14:00",
                    playlist_id=lunch_pl.id,
                )
            ],
            default_playlist_id=DEFAULT_PLAYLIST_ID,
        )
    )

    # Tuesday 12:00 → lunch rule matches.
    items = scheduled_fetch_items(
        storage, playlist_storage, schedule_storage, datetime(2026, 4, 21, 12, 0)
    )
    assert [item.id for item in items] == [text_lunch.id]

    # Tuesday 09:00 → no rule matches, default plays.
    items = scheduled_fetch_items(
        storage, playlist_storage, schedule_storage, datetime(2026, 4, 21, 9, 0)
    )
    assert [item.id for item in items] == [text_default.id]


def test_scheduled_fetch_returns_empty_for_unknown_playlist_id(tmp_path):
    """If schedule selects a playlist that doesn't exist (deleted), return
    empty so the playback loop polls instead of erroring."""
    import io as _io
    from uuid import uuid4

    from PIL import Image as _Image

    from openmarquee.content.storage import ContentStorage
    from openmarquee.playback import scheduled_fetch_items
    from openmarquee.playlist import (
        DEFAULT_PLAYLIST_ID,
        Playlist,
        PlaylistStorage,
    )
    from openmarquee.schedule import ScheduleStorage

    storage = ContentStorage(tmp_path / "content")
    playlist_storage = PlaylistStorage(tmp_path / "playlists.json")
    schedule_storage = ScheduleStorage(tmp_path / "schedules.json")

    text = TextSlide(name="x", text="x")
    img = _Image.new("RGB", (4, 4), (0, 0, 0))
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    storage.save_text_slide(text, buf.getvalue())
    default_pl = Playlist(id=DEFAULT_PLAYLIST_ID, name="default")
    default_pl.append(text.id)
    playlist_storage.set_by_id(default_pl)

    # Schedule defaults to a totally unknown playlist id.
    from openmarquee.schedule import Schedule as _Schedule

    schedule_storage.save(_Schedule(default_playlist_id=uuid4()))

    items = scheduled_fetch_items(
        storage, playlist_storage, schedule_storage, datetime(2026, 4, 21, 12, 0)
    )
    assert items == []


def test_scheduled_fetch_stamps_loop_with_active_playlist_id(tmp_path):
    """When passed a PlaybackLoop, scheduled_fetch_items publishes the
    active playlist id on it for the UI 'now playing' badge."""
    from openmarquee.content.storage import ContentStorage
    from openmarquee.playback import PlaybackLoop, scheduled_fetch_items
    from openmarquee.playlist import DEFAULT_PLAYLIST_ID, PlaylistStorage
    from openmarquee.rendering.mock import MockRenderer
    from openmarquee.schedule import ScheduleStorage

    storage = ContentStorage(tmp_path / "content")
    playlist_storage = PlaylistStorage(tmp_path / "playlists.json")
    schedule_storage = ScheduleStorage(tmp_path / "schedules.json")
    renderer = MockRenderer(8, 8, tmp_path / "out.png")

    loop = PlaybackLoop(renderer, fetch_items=lambda: [], read_asset=lambda _i: b"")
    assert loop.current_playlist_id is None

    scheduled_fetch_items(
        storage, playlist_storage, schedule_storage, datetime(2026, 4, 21, 12, 0), loop=loop
    )
    # Default schedule, default fallback id.
    assert loop.current_playlist_id == DEFAULT_PLAYLIST_ID


# --- back to existing playback tests ---


@pytest.mark.asyncio
async def test_resizes_when_asset_dimensions_differ_from_renderer(renderer):
    """Renderer is 8x8; asset is 16x16 — should be resized via NEAREST."""
    big_png = _png_bytes(16, 16, (200, 100, 50))
    slide = TextSlide(name="big", text="x", duration_ms=_FAST_DURATION_MS)
    loop = _new_loop(renderer, fetch_items=lambda: [slide], read_asset=lambda _id: big_png)
    await loop.start()
    await asyncio.sleep(0.05)
    expected = bytes((200, 100, 50)) * (renderer.width * renderer.height)
    assert renderer.last_frame == expected
    await loop.stop()


# --- auto-mode text slides (render-over at playback time) ---


@pytest.mark.asyncio
async def test_auto_mode_slide_ticks_and_reemits_frames(renderer):
    """Auto-mode text slides should push multiple frames to the renderer
    during their duration — one per auto_tick_seconds. Proves the
    re-composition path is wired and the stored PNG is NOT just
    forwarded once."""
    slide = TextSlide(
        name="clock",
        text="placeholder",
        auto_mode="time",
        auto_format="time_hms",
        duration_ms=200,
    )
    rendered = _track_frames(renderer)

    loop = _new_loop(
        renderer,
        fetch_items=lambda: [slide],
        read_asset=lambda _id: b"",  # auto slides don't read asset.png in this path
        get_timezone=lambda: "UTC",
        auto_tick_seconds=0.05,
    )
    await loop.start()
    # 200ms duration at 50ms tick → ~4 ticks plus a bit of scheduling slack.
    await asyncio.sleep(0.3)
    await loop.stop()

    # Multiple frames emitted for the single slide.
    assert len(rendered) >= 3


@pytest.mark.asyncio
async def test_auto_mode_slide_skips_asset_read(renderer):
    """Unlike a static slide, an auto slide doesn't need the stored
    asset.png — the render-over path composes from slide metadata +
    current time. A missing / raising read_asset must still let playback
    run cleanly."""
    slide = TextSlide(
        name="clock",
        text="placeholder",
        auto_mode="day",
        auto_format="day_long",
        duration_ms=100,
    )

    def hostile_read(_id):
        raise AssertionError("auto slide should not read stored PNG")

    loop = _new_loop(
        renderer,
        fetch_items=lambda: [slide],
        read_asset=hostile_read,
        get_timezone=lambda: "UTC",
        auto_tick_seconds=0.05,
    )
    await loop.start()
    await asyncio.sleep(0.15)
    await loop.stop()
    # Reached a rendered frame → the auto path ran without touching the asset.
    assert renderer.last_frame is not None


@pytest.mark.asyncio
async def test_auto_mode_exposes_metadata_on_playback_state(renderer):
    """State endpoint fields current_item_auto_mode / auto_format should
    reflect the currently-rendering auto slide — the live preview uses
    them to overlay the ticking text client-side."""
    slide = TextSlide(
        name="clock",
        text="placeholder",
        auto_mode="time",
        auto_format="time_hm",
        duration_ms=500,
    )
    loop = _new_loop(
        renderer,
        fetch_items=lambda: [slide],
        read_asset=lambda _id: b"",
        get_timezone=lambda: "UTC",
        auto_tick_seconds=0.05,
    )
    await loop.start()
    # Let the loop enter the auto slide before peeking.
    await asyncio.sleep(0.08)
    assert loop.current_item_auto_mode == "time"
    assert loop.current_item_auto_format == "time_hm"
    await loop.stop()
    # On stop, fields clear.
    assert loop.current_item_auto_mode is None
    assert loop.current_item_auto_format is None
