import asyncio
import functools
import io
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from PIL import Image

from openmarquee.content import (
    ImageSlide,
    StreamSlide,
    TextLayer,
    TextSlide,
    WebSlide,
)


def _text_slide(*, name="x", text="x", **kwargs) -> TextSlide:
    """Build a single-layer TextSlide for tests. Schema v3 routed
    text fields off the slide root into text_layers — accept the flat
    kwargs the existing tests use and shuttle them into the canonical
    layer."""
    layer_keys = {
        "text_color", "font_family", "font_size_px",
        "font_size_pct", "auto_mode", "auto_format", "box",
    }
    layer = {"text": text}
    for k in list(kwargs.keys()):
        if k in layer_keys:
            layer[k] = kwargs.pop(k)
    return TextSlide(
        name=name,
        text_layers=[TextLayer(**layer)],
        **kwargs,
    )
from openmarquee.playback import PlaybackLoop, web_refresh_due
from openmarquee.rendering.mock import MockRenderer
from openmarquee.stream_consumer import StreamConsumer
from tests.test_stream_consumer import _write_mock_ffmpeg

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
    active_playlist_id=None,
    web_screenshot_producer=None,
):
    return PlaybackLoop(
        renderer,
        fetch_items=fetch_items,
        read_asset=read_asset,
        empty_playlist_poll_seconds=_FAST_EMPTY_POLL,
        get_timezone=get_timezone,
        auto_tick_seconds=auto_tick_seconds,
        active_playlist_id=active_playlist_id,
        web_screenshot_producer=web_screenshot_producer,
    )


def _png_bytes(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_slide(name: str, color: tuple[int, int, int]) -> tuple[TextSlide, bytes]:
    slide = _text_slide(name=name, text=name, duration_ms=_FAST_DURATION_MS)
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
async def test_single_item_drives_renderer_via_begin_slide(renderer):
    """DELETE-PIL slice 11: MockRenderer is IPC-shaped now, so the loop
    drives it via begin_slide instead of pushing frames through
    render_frame. Assert the loop called begin_slide for our slide."""
    slide, png = _make_slide("a", (255, 0, 0))
    loop = _new_loop(renderer, fetch_items=lambda: [slide], read_asset=lambda _id: png)
    await loop.start()
    await asyncio.sleep(0.05)
    assert any(c[0] == slide.id for c in renderer.begin_slide_calls)
    assert loop.current_item_id == slide.id
    await loop.stop()


@pytest.mark.asyncio
async def test_cycles_through_multiple_items_in_order(renderer):
    """The loop drives begin_slide once per slide in the playlist
    order. Two slides at 100ms each + slack → both seen in order."""
    slide_a, png_a = _make_slide("a", (255, 0, 0))
    slide_b, png_b = _make_slide("b", (0, 255, 0))
    items = [slide_a, slide_b]
    assets = {slide_a.id: png_a, slide_b.id: png_b}

    loop = _new_loop(
        renderer,
        fetch_items=lambda: items,
        read_asset=lambda item_id: assets[item_id],
    )
    await loop.start()
    await asyncio.sleep(0.25)
    await loop.stop()

    slide_ids_seen = [c[0] for c in renderer.begin_slide_calls]
    assert slide_a.id in slide_ids_seen
    assert slide_b.id in slide_ids_seen
    # First call is slide A (playlist order).
    assert slide_ids_seen[0] == slide_a.id


@pytest.mark.asyncio
async def test_stop_returns_promptly_during_long_duration(renderer):
    long_slide = _text_slide(name="long", duration_ms=10_000)
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
    """The loop re-fetches items on each iteration. Adding a slide
    mid-loop should result in begin_slide firing for it on the next
    iteration."""
    items_box: list[TextSlide] = []
    assets: dict[UUID, bytes] = {}

    loop = _new_loop(
        renderer,
        fetch_items=lambda: list(items_box),
        read_asset=lambda item_id: assets[item_id],
    )
    await loop.start()
    await asyncio.sleep(0.03)  # several empty-poll iterations
    assert renderer.begin_slide_calls == []

    slide, png = _make_slide("late", (255, 128, 0))
    items_box.append(slide)
    assets[slide.id] = png

    await asyncio.sleep(0.05)  # next iteration picks it up
    assert any(c[0] == slide.id for c in renderer.begin_slide_calls)
    await loop.stop()


@pytest.mark.asyncio
async def test_schedule_switch_preempts_running_playlist(renderer):
    """Bug 1: switching the active playlist mid-loop preempts the
    running playlist instead of waiting for it to finish its loop."""
    pl_a, pl_b = uuid4(), uuid4()
    a_slides = [
        _text_slide(name=f"a{i}", text=f"a{i}", duration_ms=300)
        for i in range(3)
    ]
    b0 = _text_slide(name="b0", text="b0", duration_ms=100)
    png = _png_bytes(8, 8, (10, 20, 30))
    assets = {s.id: png for s in (*a_slides, b0)}
    playlists = {pl_a: a_slides, pl_b: [b0]}
    active = {"id": pl_a}
    holder: dict = {}

    def fetch():
        # Mirror scheduled_fetch_items: stamp the active playlist id.
        holder["loop"]._stamp_playlist_id(active["id"])
        return list(playlists[active["id"]])

    loop = _new_loop(
        renderer,
        fetch_items=fetch,
        read_asset=lambda i: assets[i],
        active_playlist_id=lambda: active["id"],
    )
    holder["loop"] = loop
    await loop.start()
    await asyncio.sleep(0.15)  # a0 (300ms) mid-play
    active["id"] = pl_b  # operator switches the active schedule
    await asyncio.sleep(0.75)  # a0 finishes (~0.3s), preempt, b0 cycles
    await loop.stop()

    seen = [c[0] for c in renderer.begin_slide_calls]
    assert b0.id in seen, "new playlist's slide must render after the switch"
    # Preemption: the old playlist's later slides must NOT have played —
    # the switch broke the pass before reaching them.
    assert a_slides[1].id not in seen
    assert a_slides[2].id not in seen


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
async def test_loop_recovers_from_renderer_op_failure(renderer):
    """If a renderer IPC op raises, the loop should log and keep
    going. Hook begin_slide to raise for slide A; slide B should
    still get its begin_slide call."""
    slide_a, png_a = _make_slide("a", (10, 20, 30))
    slide_b, png_b = _make_slide("b", (40, 50, 60))
    assets = {slide_a.id: png_a, slide_b.id: png_b}

    fail_for = {slide_a.id}
    original_begin = renderer.begin_slide

    def explosive_begin(slide_id, t0_ms, duration_ms):
        if slide_id in fail_for:
            raise RuntimeError("renderer fell over")
        original_begin(slide_id, t0_ms, duration_ms)

    loop = _new_loop(
        renderer,
        fetch_items=lambda: [slide_a, slide_b],
        read_asset=lambda item_id: assets[item_id],
    )
    renderer.begin_slide = explosive_begin  # type: ignore[method-assign]

    await loop.start()
    # Per-slide failure adds a 250ms throttle settle in _loop; both
    # slides plus that settle need ~600ms to complete.
    await asyncio.sleep(0.7)
    await loop.stop()

    # The successful slide(s) made it through; the failing one was
    # caught by the per-slide try/except + throttle in _loop.
    saw_b_via_original = any(
        c[0] == slide_b.id for c in renderer.begin_slide_calls
    )
    assert saw_b_via_original


@pytest.mark.asyncio
async def test_all_unplayable_playlist_backs_off_and_recovers(renderer):
    """Bug 8 gap (2026-05-20): a playlist whose every slide is
    unplayable — here a 1-item playlist of an unsupported-kind slide,
    qarl's "coffe" one-bad-video playlist — must NOT hot-spin. The
    skip rail "advance to the next slide" lands back on the same bad
    slide, so without a floor the loop re-fetches + re-iterates at
    render-thread speed and freezes the sign.

    Assert: the loop is rate-limited to the stuck-backoff floor while
    stuck, and recovers when playable content returns."""
    from openmarquee.rendering.rust_renderer import (
        RustRendererUnsupportedSlideError,
    )

    bad_slide, bad_png = _make_slide("bad", (10, 20, 30))
    good_slide, good_png = _make_slide("good", (40, 50, 60))
    assets = {bad_slide.id: bad_png, good_slide.id: good_png}
    state = {"playlist": [bad_slide]}

    begin_calls: list[UUID] = []
    original_begin = renderer.begin_slide

    def begin(slide_id, t0_ms, duration_ms):
        begin_calls.append(slide_id)
        if slide_id == bad_slide.id:
            raise RustRendererUnsupportedSlideError(
                "video slide unsupported (load failed)"
            )
        original_begin(slide_id, t0_ms, duration_ms)

    renderer.begin_slide = begin  # type: ignore[method-assign]
    backoff = 0.1
    loop = PlaybackLoop(
        renderer,
        fetch_items=lambda: state["playlist"],
        read_asset=lambda i: assets[i],
        empty_playlist_poll_seconds=_FAST_EMPTY_POLL,
        auto_tick_seconds=0.02,
        stuck_backoff_seconds=backoff,
    )

    await loop.start()
    # Stuck phase: the playlist holds only the unplayable slide.
    await asyncio.sleep(0.55)
    stuck_calls = len(begin_calls)
    # With a 0.1s backoff floor, ~0.55s of stuck loop is at most a
    # handful of passes. Pre-fix the loop spun at render-thread speed
    # (hundreds-to-thousands of begin_slide calls). The generous
    # ceiling cleanly separates fixed from broken.
    assert stuck_calls <= 12, (
        f"hot-spin: {stuck_calls} begin_slide calls in 0.55s — the "
        f"{backoff}s backoff floor should bound this to ~5-6"
    )

    # Recovery: a playable slide appears in the playlist. The loop
    # re-fetches every iteration (just rate-limited while stuck), so
    # it must pick the good slide up and play it.
    state["playlist"] = [good_slide]
    await asyncio.sleep(0.4)
    await loop.stop()
    assert good_slide.id in begin_calls, (
        "loop did not recover when playable content returned"
    )


def _track_frames(renderer):
    """Wrap renderer.render_frame so the test can inspect every frame the loop
    pushes (not just the last one)."""
    rendered: list[bytes] = []
    original = renderer.render_frame

    def track(frame: bytes, **kwargs) -> None:
        rendered.append(frame)
        original(frame, **kwargs)

    renderer.render_frame = track  # type: ignore[method-assign]
    return rendered


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


def test_scheduled_fetch_uses_default_when_schedule_is_empty(tmp_path):
    """No schedule rules → falls back to default_playlist_id."""
    import io as _io

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
    text_in_default = _text_slide(name="in default")
    text_in_lunch = _text_slide(name="in lunch")

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

    text_default = _text_slide(name="default")
    text_lunch = _text_slide(name="lunch")

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

    text = _text_slide(name="x")
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
async def test_auto_mode_slide_drives_renderer_until_complete(renderer):
    """Auto-mode text slides drive the IPC renderer through begin_slide
    + multiple advance ticks until the slide's duration elapses. Post-
    DELETE-PIL the sidecar owns per-tick auto-mode redraw -- Python no
    longer re-pushes frames -- so the observable Python signal is the
    advance-call cadence."""
    slide = _text_slide(
        name="clock",
        text="placeholder",
        auto_mode="time",
        auto_format="time_hms",
        duration_ms=200,
    )

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

    # begin_slide fired once, advance ticked multiple times across the
    # slide's duration window.
    assert len(renderer.begin_slide_calls) >= 1
    assert len(renderer.advance_calls) >= 3


@pytest.mark.asyncio
async def test_auto_mode_slide_skips_asset_read(renderer):
    """Unlike a static slide, an auto slide doesn't need the stored
    asset.png — the render-over path composes from slide metadata +
    current time. A missing / raising read_asset must still let playback
    run cleanly."""
    slide = _text_slide(
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
    slide = _text_slide(
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


# --- STREAM/VLC Mode B: StreamSlide playback (slice 7) ---------------------


def _patch_stream_ffmpeg(
    monkeypatch,
    ffmpeg_bin: str,
    *,
    source_size: tuple[int, int] = (8, 8),
) -> None:
    """Point the playback loop's StreamConsumer at a mock-ffmpeg
    binary (or a missing path, to simulate an unreachable stream).

    HW-decode (2026-05-20): the consumer ffprobes for the source
    resolution; inject `source_size` so the probe is skipped (no real
    ffprobe against the test's fake stream URL). The consumer's NV12
    frame size is then `src_w*src_h*3//2`."""
    monkeypatch.setattr(
        "openmarquee.playback.StreamConsumer",
        functools.partial(
            StreamConsumer, ffmpeg_bin=ffmpeg_bin, source_size=source_size
        ),
    )


@pytest.mark.asyncio
async def test_stream_slide_pumps_frames_to_renderer(
    renderer, tmp_path, monkeypatch
):
    """A StreamSlide in the playlist is intercepted before the IPC
    path; its (mock) stream frames are pushed straight to the renderer.

    HW-decode (2026-05-20): the consumer emits source-resolution NV12
    (8x8 -> 96-byte NV12 frames here), and the pump threads the NV12
    pixel_format + source dims into render_frame()."""
    # NV12 frame size for the injected 8x8 source.
    frame_size = 8 * 8 * 3 // 2
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=frame_size, n_frames=5
    )
    _patch_stream_ffmpeg(monkeypatch, mock, source_size=(8, 8))
    captured: list[bytes] = []
    captured_formats: list[str] = []
    original = renderer.render_frame

    def _record(d, **kwargs):
        captured.append(d)
        captured_formats.append(kwargs.get("pixel_format", "rgb888"))
        return original(d, **kwargs)

    renderer.render_frame = _record

    # 2s duration: the first-frame wait is bounded by min(connect-
    # timeout, slot remaining), so a too-short slot would starve the
    # budget below the mock python-interpreter's spawn time.
    slide = StreamSlide(
        name="live", stream_url="rtsp://h:8554/x", duration_ms=2000
    )
    loop = _new_loop(
        renderer, fetch_items=lambda: [slide], read_asset=lambda _id: b""
    )
    await loop.start()
    await asyncio.sleep(0.5)
    await loop.stop()

    assert len(captured) >= 5
    assert all(len(f) == frame_size for f in captured)
    # The mock fills frame i with the byte value i — confirms ordering
    # and that the renderer received the stream frames intact.
    assert captured[0] == bytes([0]) * frame_size
    assert captured[4] == bytes([4]) * frame_size
    # HW-decode: the consumer's frames are NV12; the pump threads that
    # format into render_frame().
    assert all(fmt == "nv12" for fmt in captured_formats)
    # STREAM/VLC slice 2.5: the stream pump ends the renderer's
    # frame-pump session on every slot exit (once per playlist cycle).
    assert renderer.end_external_frames_calls >= 1


@pytest.mark.asyncio
async def test_stream_unreachable_skip_advances_immediately(
    renderer, tmp_path, monkeypatch
):
    """on_unreachable='skip' — an unreachable StreamSlide is
    abandoned at once; the loop reaches the next slide rather than
    holding the dead slot for its full duration."""
    # A missing ffmpeg binary == spawn fails == zero frames.
    _patch_stream_ffmpeg(monkeypatch, str(tmp_path / "no-such-ffmpeg"))
    stream = StreamSlide(
        name="dead",
        stream_url="rtsp://h/x",
        duration_ms=10_000,
        on_unreachable="skip",
    )
    text, png = _make_slide("after", (0, 255, 0))
    loop = _new_loop(
        renderer,
        fetch_items=lambda: [stream, text],
        read_asset=lambda _id: png,
    )
    await loop.start()
    await asyncio.sleep(0.3)
    seen = [c[0] for c in renderer.begin_slide_calls]
    await loop.stop()
    # The 10s stream slide was skipped near-instantly — the text slide
    # after it was reached well inside 0.3s.
    assert text.id in seen


@pytest.mark.asyncio
async def test_stream_unreachable_hold_waits_out_the_slot(
    renderer, tmp_path, monkeypatch
):
    """on_unreachable='hold_last_frame' — an unreachable StreamSlide
    still occupies its full slot; the loop does NOT advance early."""
    _patch_stream_ffmpeg(monkeypatch, str(tmp_path / "no-such-ffmpeg"))
    stream = StreamSlide(
        name="dead",
        stream_url="rtsp://h/x",
        duration_ms=10_000,
        on_unreachable="hold_last_frame",
    )
    text, png = _make_slide("after", (0, 255, 0))
    loop = _new_loop(
        renderer,
        fetch_items=lambda: [stream, text],
        read_asset=lambda _id: png,
    )
    await loop.start()
    await asyncio.sleep(0.3)
    seen = [c[0] for c in renderer.begin_slide_calls]
    await loop.stop()
    # The stream slot is being held for its 10s duration — the text
    # slide after it is NOT reached.
    assert text.id not in seen


@pytest.mark.asyncio
async def test_stream_unreachable_black_paints_a_black_frame(
    renderer, tmp_path, monkeypatch
):
    """on_unreachable='black' paints one all-zero RGB frame before
    holding the slot."""
    _patch_stream_ffmpeg(monkeypatch, str(tmp_path / "no-such-ffmpeg"))
    captured: list[bytes] = []
    original = renderer.render_frame

    def _record(d, **kwargs):
        captured.append(d)
        return original(d, **kwargs)

    renderer.render_frame = _record
    stream = StreamSlide(
        name="dead",
        stream_url="rtsp://h/x",
        duration_ms=300,
        on_unreachable="black",
    )
    loop = _new_loop(
        renderer, fetch_items=lambda: [stream], read_asset=lambda _id: b""
    )
    await loop.start()
    await asyncio.sleep(0.2)
    await loop.stop()
    assert bytes(8 * 8 * 3) in captured


@pytest.mark.asyncio
async def test_stream_connect_timeout_falls_back(
    renderer, tmp_path, monkeypatch
):
    """If ffmpeg spawns but delivers no frame within the connect
    timeout, the slide falls back to on_unreachable rather than
    blocking on the dead stream for the whole slot."""
    monkeypatch.setattr("openmarquee.playback._STREAM_CONNECT_TIMEOUT_S", 0.2)
    # hang mock: spawns, emits 0 frames, then sleeps — ffmpeg is up but
    # never produces video, exactly how an unreachable stream URL behaves.
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=8 * 8 * 3, n_frames=0, hang=True
    )
    _patch_stream_ffmpeg(monkeypatch, mock)
    stream = StreamSlide(
        name="stuck",
        stream_url="rtsp://h/x",
        duration_ms=10_000,
        on_unreachable="skip",
    )
    text, png = _make_slide("after", (0, 255, 0))
    loop = _new_loop(
        renderer,
        fetch_items=lambda: [stream, text],
        read_asset=lambda _id: png,
    )
    await loop.start()
    await asyncio.sleep(0.6)
    seen = [c[0] for c in renderer.begin_slide_calls]
    await loop.stop()
    # The 0.2s connect timeout fired, skip advanced — the text slide
    # was reached well inside 0.6s despite the 10s nominal duration.
    assert text.id in seen


@pytest.mark.asyncio
async def test_stream_slide_preempted_by_pause(
    renderer, tmp_path, monkeypatch
):
    """A pause() during a StreamSlide slot is honored — the loop
    yields the renderer and saves the resume index, so a stream
    takeover can preempt a stream slot."""
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=8 * 8 * 3, n_frames=0, continuous=True
    )
    _patch_stream_ffmpeg(monkeypatch, mock)
    stream = StreamSlide(
        name="live", stream_url="rtsp://h/x", duration_ms=10_000
    )
    loop = _new_loop(
        renderer, fetch_items=lambda: [stream], read_asset=lambda _id: b""
    )
    await loop.start()
    await asyncio.sleep(0.15)  # let the pump start streaming frames
    await loop.pause()
    await asyncio.sleep(0.1)
    assert loop.is_paused
    assert loop._resume_at_index == 0
    await loop.resume()
    await loop.stop()


# --- Web slide: refresh staleness + non-blocking kick (Web slide P3) -------


def test_web_refresh_due_first_fetch_is_always_due():
    """No prior fetch (None) -> a refresh is due on first sight."""
    assert web_refresh_due(None, now_monotonic=100.0, refresh_interval_s=300)


def test_web_refresh_due_fresh_slide_is_not_due():
    """A slide fetched less than refresh_interval_s ago is NOT due."""
    # Fetched at t=100, interval 300s, now t=250 -> 150s elapsed < 300.
    assert not web_refresh_due(
        100.0, now_monotonic=250.0, refresh_interval_s=300
    )


def test_web_refresh_due_stale_slide_is_due():
    """A slide whose last fetch is older than refresh_interval_s IS due."""
    # Fetched at t=100, interval 300s, now t=500 -> 400s elapsed >= 300.
    assert web_refresh_due(
        100.0, now_monotonic=500.0, refresh_interval_s=300
    )


def test_web_refresh_due_exactly_at_interval_is_due():
    """Elapsed exactly equal to the interval counts as due."""
    assert web_refresh_due(
        100.0, now_monotonic=400.0, refresh_interval_s=300
    )


@pytest.mark.asyncio
async def test_web_slide_kicks_refresh_producer(renderer):
    """Entering a Web slide's slot fires the screenshot producer."""
    web = WebSlide(
        name="status", url="https://h/x", duration_ms=_FAST_DURATION_MS,
        refresh_interval_s=10,
    )
    calls: list[tuple] = []

    async def producer(slide, width, height) -> bool:
        calls.append((slide.id, width, height))
        return True

    loop = _new_loop(
        renderer,
        fetch_items=lambda: [web],
        read_asset=lambda _id: _png_bytes(8, 8, (1, 2, 3)),
        web_screenshot_producer=producer,
    )
    await loop.start()
    await asyncio.sleep(0.1)
    await loop.stop()
    # The producer was kicked at least once with the slide id + the
    # renderer's panel dimensions.
    assert calls
    assert calls[0] == (web.id, renderer.width, renderer.height)


@pytest.mark.asyncio
async def test_web_slide_slot_does_not_await_the_fetch(renderer):
    """CRITICAL: entering a Web slot must NOT block on the screenshot
    fetch. With a producer that hangs forever, the slide still plays
    (begin_slide fires) well within the slot — proving create_task,
    not await."""
    web = WebSlide(
        name="status", url="https://h/x", duration_ms=_FAST_DURATION_MS,
        refresh_interval_s=10,
    )
    started = asyncio.Event()

    async def hanging_producer(slide, width, height) -> bool:
        started.set()
        # Never returns within the test window — if _loop awaited
        # this, the slide would never render.
        await asyncio.sleep(3600)
        return True

    loop = _new_loop(
        renderer,
        fetch_items=lambda: [web],
        read_asset=lambda _id: _png_bytes(8, 8, (4, 5, 6)),
        web_screenshot_producer=hanging_producer,
    )
    await loop.start()
    # The slide rendered despite the producer hanging — the loop did
    # not await the fetch.
    await asyncio.sleep(0.15)
    seen = [c[0] for c in renderer.begin_slide_calls]
    inflight = set(loop._web_inflight)
    await loop.stop()
    assert web.id in seen, "web slide never rendered — _loop blocked on fetch"
    # The hanging fetch is still tracked in-flight (it never finished).
    assert started.is_set()
    assert web.id in inflight


@pytest.mark.asyncio
async def test_web_slide_inflight_fetch_is_not_re_kicked(renderer):
    """While a fetch is in flight for a slide, re-entering its slot
    does NOT kick a second fetch (the in-flight guard)."""
    web = WebSlide(
        name="status", url="https://h/x", duration_ms=_FAST_DURATION_MS,
        # Tiny interval so staleness alone would re-kick every slot.
        refresh_interval_s=10,
    )
    call_count = 0
    release = asyncio.Event()

    async def slow_producer(slide, width, height) -> bool:
        nonlocal call_count
        call_count += 1
        await release.wait()
        return True

    loop = _new_loop(
        renderer,
        fetch_items=lambda: [web],
        read_asset=lambda _id: _png_bytes(8, 8, (7, 8, 9)),
        web_screenshot_producer=slow_producer,
    )
    await loop.start()
    # Several slot cycles pass (100ms duration each) while the first
    # fetch is still blocked — no second kick should have happened.
    await asyncio.sleep(0.4)
    count_while_inflight = call_count
    release.set()
    await asyncio.sleep(0.05)
    await loop.stop()
    assert count_while_inflight == 1, (
        f"expected exactly one in-flight fetch, got {count_while_inflight}"
    )


@pytest.mark.asyncio
async def test_web_slide_renders_without_a_producer(renderer):
    """A Web slide with no producer wired still plays as an image
    slide (renders its current asset.png), no crash."""
    web = WebSlide(
        name="status", url="https://h/x", duration_ms=_FAST_DURATION_MS,
    )
    loop = _new_loop(
        renderer,
        fetch_items=lambda: [web],
        read_asset=lambda _id: _png_bytes(8, 8, (10, 11, 12)),
        web_screenshot_producer=None,
    )
    await loop.start()
    await asyncio.sleep(0.1)
    seen = [c[0] for c in renderer.begin_slide_calls]
    await loop.stop()
    assert web.id in seen


# --- C3/M1: prune the per-slide Web-refresh tracking dicts -----------------


@pytest.mark.asyncio
async def test_web_last_fetch_pruned_when_slide_leaves_playlist(renderer):
    """C3/M1: once the playlist no longer contains a Web slide id, the
    next loop pass prunes that id out of _web_last_fetch — no unbounded
    leak as a sign churns through Web slides over months."""
    web_a = WebSlide(
        name="a", url="https://h/a", duration_ms=_FAST_DURATION_MS,
        refresh_interval_s=10,
    )
    web_b = WebSlide(
        name="b", url="https://h/b", duration_ms=_FAST_DURATION_MS,
        refresh_interval_s=10,
    )

    async def producer(slide, width, height) -> bool:
        return True

    # The playlist starts with both Web slides, then drops web_a.
    items = {"current": [web_a, web_b]}
    loop = _new_loop(
        renderer,
        fetch_items=lambda: items["current"],
        read_asset=lambda _id: _png_bytes(8, 8, (1, 2, 3)),
        web_screenshot_producer=producer,
    )
    await loop.start()
    await asyncio.sleep(0.15)
    # Both slides have a tracking entry while both are in the playlist.
    assert web_a.id in loop._web_last_fetch
    assert web_b.id in loop._web_last_fetch
    # Drop web_a from the playlist; the next outer pass prunes it.
    items["current"] = [web_b]
    await asyncio.sleep(0.15)
    pruned = dict(loop._web_last_fetch)
    await loop.stop()
    assert web_a.id not in pruned, "departed Web slide id leaked in _web_last_fetch"
    assert web_b.id in pruned, "still-present Web slide id wrongly pruned"


@pytest.mark.asyncio
async def test_inflight_id_not_pruned_when_slide_leaves_playlist(renderer):
    """C3/M1: a Web slide id whose fetch is still IN FLIGHT must NOT be
    pruned from _web_inflight even after it leaves the playlist —
    pruning a genuinely-running id would re-enable a double-kick. The
    set self-cleans via the kick's done-callback when the task ends."""
    web = WebSlide(
        name="status", url="https://h/x", duration_ms=_FAST_DURATION_MS,
        refresh_interval_s=10,
    )
    release = asyncio.Event()

    async def hanging_producer(slide, width, height) -> bool:
        # Stays in flight until released — so its id sits in
        # _web_inflight across the playlist change below.
        await release.wait()
        return True

    items = {"current": [web]}
    loop = _new_loop(
        renderer,
        fetch_items=lambda: items["current"],
        read_asset=lambda _id: _png_bytes(8, 8, (4, 5, 6)),
        web_screenshot_producer=hanging_producer,
    )
    await loop.start()
    await asyncio.sleep(0.15)
    assert web.id in loop._web_inflight  # fetch kicked, still running
    # Remove the Web slide from the playlist while its fetch is in
    # flight; the prune pass runs (empty playlist) but must leave the
    # in-flight id alone.
    items["current"] = []
    await asyncio.sleep(0.15)
    assert web.id in loop._web_inflight, (
        "in-flight Web slide id was pruned — would re-enable double-kick"
    )
    # Releasing the fetch lets the done-callback self-clean the id.
    release.set()
    await asyncio.sleep(0.05)
    assert web.id not in loop._web_inflight
    await loop.stop()


@pytest.mark.asyncio
async def test_kick_web_refresh_now_fires_an_immediate_fetch(renderer):
    """Bug W1: kick_web_refresh_now fires the producer immediately,
    bypassing the staleness check — used by the create/update API
    handlers so a new/changed Web slide gets a real asset promptly."""
    web = WebSlide(
        name="status", url="https://h/x", duration_ms=_FAST_DURATION_MS,
        # A long interval — web_refresh_due would say "not due" if it
        # had ever been stamped; kick_web_refresh_now ignores it.
        refresh_interval_s=86400,
    )
    calls: list[tuple] = []

    async def producer(slide, width, height) -> bool:
        calls.append((slide.id, width, height))
        return True

    loop = _new_loop(
        renderer,
        fetch_items=lambda: [],
        read_asset=lambda _id: _png_bytes(8, 8, (1, 2, 3)),
        web_screenshot_producer=producer,
    )
    loop.kick_web_refresh_now(web)
    await asyncio.sleep(0.05)
    # The producer ran once with the slide id + the renderer's dims.
    assert calls == [(web.id, renderer.width, renderer.height)]


@pytest.mark.asyncio
async def test_kick_web_refresh_now_does_not_block_on_a_hanging_fetch(
    renderer,
):
    """Bug W1: kick_web_refresh_now is fire-and-forget — it returns
    IMMEDIATELY even if the producer hangs forever (a slow/dead render
    helper must never delay the create/update HTTP response)."""
    web = WebSlide(
        name="status", url="https://h/x", duration_ms=_FAST_DURATION_MS,
    )
    started = asyncio.Event()

    async def hanging_producer(slide, width, height) -> bool:
        started.set()
        await asyncio.sleep(3600)
        return True

    loop = _new_loop(
        renderer,
        fetch_items=lambda: [],
        read_asset=lambda _id: _png_bytes(8, 8, (4, 5, 6)),
        web_screenshot_producer=hanging_producer,
    )
    # Returns synchronously — no await — despite the hanging producer.
    loop.kick_web_refresh_now(web)
    await asyncio.sleep(0.05)
    assert started.is_set()
    # The hanging fetch is tracked in-flight; it never finished.
    assert web.id in loop._web_inflight


@pytest.mark.asyncio
async def test_kick_web_refresh_now_no_producer_is_a_noop(renderer):
    """Bug W1: with no producer wired (test/standalone configs),
    kick_web_refresh_now is a clean no-op — it must not raise."""
    web = WebSlide(name="status", url="https://h/x")
    loop = _new_loop(
        renderer,
        fetch_items=lambda: [],
        read_asset=lambda _id: _png_bytes(8, 8, (1, 1, 1)),
        web_screenshot_producer=None,
    )
    # No producer — does nothing, raises nothing.
    loop.kick_web_refresh_now(web)
    assert web.id not in loop._web_inflight


@pytest.mark.asyncio
async def test_kick_web_refresh_now_failed_producer_does_not_raise(renderer):
    """Bug W1 edge: a producer that raises (render helper unreachable)
    must not surface as an unretrieved-task exception — the kick's
    done-callback consumes it and clears the in-flight id."""
    web = WebSlide(name="status", url="https://h/down")

    async def failing_producer(slide, width, height) -> bool:
        raise RuntimeError("render helper unreachable")

    loop = _new_loop(
        renderer,
        fetch_items=lambda: [],
        read_asset=lambda _id: _png_bytes(8, 8, (1, 1, 1)),
        web_screenshot_producer=failing_producer,
    )
    loop.kick_web_refresh_now(web)
    await asyncio.sleep(0.05)
    # The crashed task's done-callback cleared the in-flight id, so a
    # subsequent kick can still proceed — no wedged id, no raise.
    assert web.id not in loop._web_inflight
