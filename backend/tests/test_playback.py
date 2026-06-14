import asyncio
import contextlib
import functools
import io
import logging
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from PIL import Image

from openmarquee.content import (
    StreamSlide,
    TextLayer,
    TextSlide,
    WebSlide,
)
from openmarquee.playback import PlaybackLoop, web_refresh_due
from openmarquee.rendering.mock import MockRenderer
from openmarquee.stream_consumer import StreamConsumer
from tests.test_stream_consumer import _write_mock_ffmpeg


def _text_slide(*, name="x", text="x", **kwargs) -> TextSlide:
    """Build a single-layer TextSlide for tests. Schema v3 routed
    text fields off the slide root into text_layers — accept the flat
    kwargs the existing tests use and shuttle them into the canonical
    layer."""
    layer_keys = {
        "text_color",
        "font_family",
        "font_size_px",
        "font_size_pct",
        "auto_mode",
        "auto_format",
        "box",
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
    a_slides = [_text_slide(name=f"a{i}", text=f"a{i}", duration_ms=300) for i in range(3)]
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
    the loop should treat the iteration as empty and try again.

    r9 (2026-05-26) flake fix: pre-fix used `await asyncio.sleep(0.1)`
    as the wait-for-recovery boundary, which assumes 3 fetch_items
    calls + 2×10ms empty-polls + 1 paint fit inside 100ms. Under
    full-backend-suite load on the Mac dev box, asyncio scheduling
    jitter stretches each empty-poll's `_wait` past the budget and
    the assertion fired before the recovery paint happened. The fix
    polls for the side effect (calls["n"] >= 3 AND
    renderer.last_frame is not None) with a generous outer timeout
    — completes as soon as the recovery is observable, but doesn't
    falsely fail under jitter. See r9 commit body for the full
    rationale; cost was 3 --no-verify slips on code1's r1 push chain
    tonight.
    """
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
    try:
        # Wait for the recovery cycle to complete: 3 fetch_items calls
        # (2 raise + 1 success) AND a paint that populates
        # renderer.last_frame. 1s outer timeout is the regression
        # guard — if the loop genuinely fails to recover (a real bug),
        # the test still fails with a useful AssertionError; under
        # any healthy environment the recovery is observable in
        # ~30-50ms regardless of how long pytest's full-suite jitter
        # stretches each sleep slice.
        deadline = asyncio.get_event_loop().time() + 1.0
        while asyncio.get_event_loop().time() < deadline:
            if calls["n"] >= 3 and renderer.last_frame is not None:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError(
                "playback loop did not recover within 1s: "
                f"calls={calls['n']} "
                f"last_frame={'set' if renderer.last_frame is not None else 'None'}"
            )
    finally:
        await loop.stop()

    # The loop survived the failures and ultimately rendered. These
    # assertions are now redundant in the happy path (the while-loop
    # already required them to break) but stay for documentation +
    # to lock the post-stop state.
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
    saw_b_via_original = any(c[0] == slide_b.id for c in renderer.begin_slide_calls)
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
            raise RustRendererUnsupportedSlideError("video slide unsupported (load failed)")
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
    assert good_slide.id in begin_calls, "loop did not recover when playable content returned"


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
    active_id, items = scheduled_fetch_items(
        storage, playlist_storage, schedule_storage, datetime(2026, 4, 21, 12, 0)
    )
    assert active_id == DEFAULT_PLAYLIST_ID
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
    active_id, items = scheduled_fetch_items(
        storage, playlist_storage, schedule_storage, datetime(2026, 4, 21, 12, 0)
    )
    assert active_id == lunch_pl.id
    assert [item.id for item in items] == [text_lunch.id]

    # Tuesday 09:00 → no rule matches, default plays.
    active_id, items = scheduled_fetch_items(
        storage, playlist_storage, schedule_storage, datetime(2026, 4, 21, 9, 0)
    )
    assert active_id == DEFAULT_PLAYLIST_ID
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

    active_id, items = scheduled_fetch_items(
        storage, playlist_storage, schedule_storage, datetime(2026, 4, 21, 12, 0)
    )
    # active_id is the (deleted) playlist id from the schedule -- caller
    # gets visibility into "what was selected" even when the resolution
    # yields no items, so the stamp side-effect downstream still updates.
    assert items == []


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
        functools.partial(StreamConsumer, ffmpeg_bin=ffmpeg_bin, source_size=source_size),
    )


@pytest.mark.asyncio
async def test_stream_slide_render_frame_off_loop_allows_concurrent_progress(
    renderer, tmp_path, monkeypatch
):
    """Perf-night r5 (2026-05-26) load-bearing invariant: the
    asyncio.to_thread wrap around `renderer.render_frame` inside the
    `_play_stream_slide` pump loop (playback.py:1248) must release the
    asyncio event loop while the renderer is mid-IPC. Mirrors the
    r4 pattern for the `_play_via_rust_ipc` advance wrap.

    Setup:
      - mock-ffmpeg delivers 5 NV12 frames
      - renderer.render_frame is wrapped to inject a 30ms `time.sleep`
        per call (simulating the slice-2.5 push-frames IPC blocking
        readline)
      - a counter coroutine increments every 5ms

    Pre-fix (bare `renderer.render_frame(...)`), each 30ms sleep
    blocks the asyncio loop entirely; across 5 frames the counter
    would barely tick (~0-1 increments).

    Post-fix (`await asyncio.to_thread(renderer.render_frame, ...)`),
    each sleep happens on a worker thread; the counter at 5ms
    cadence sees ~30 increments during 5×30ms of pump time. Floor
    of 5 = conservative regression lock for slow CI runners."""
    import time as _time

    frame_size = 8 * 8 * 3 // 2  # NV12 at 8x8
    mock = _write_mock_ffmpeg(tmp_path / "ffmpeg", frame_size=frame_size, n_frames=5)
    _patch_stream_ffmpeg(monkeypatch, mock, source_size=(8, 8))

    real_render_frame = renderer.render_frame

    def slow_render_frame(d, **kwargs):
        _time.sleep(0.03)  # 30ms sync wedge per frame
        return real_render_frame(d, **kwargs)

    renderer.render_frame = slow_render_frame

    counter = 0

    async def tick_counter():
        nonlocal counter
        try:
            while True:
                await asyncio.sleep(0.005)
                counter += 1
        except asyncio.CancelledError:
            return

    slide = StreamSlide(name="live", stream_url="rtsp://h:8554/x", duration_ms=2000)
    loop = _new_loop(renderer, fetch_items=lambda: [slide], read_asset=lambda _id: b"")
    counter_task = asyncio.create_task(tick_counter())
    try:
        await loop.start()
        # Long enough for the mock ffmpeg to spawn + deliver all 5
        # frames (each gated by the 30ms sleep on render_frame).
        await asyncio.sleep(1.0)
        await loop.stop()
        # Post-fix: each render_frame's 30ms sleep runs on a worker
        # thread; tick_counter freely accumulates during those windows.
        # Pre-fix: 5 frames × 30ms = 150ms of total event-loop wedge
        # in tight succession; counter would near-zero through that
        # window. > 5 is the regression-lock floor.
        assert counter > 5, (
            f"tick_counter only reached {counter} during the StreamSlide "
            "pump — playback loop appears wedged during render_frame "
            "calls. asyncio.to_thread wrap likely reverted or missing "
            "from playback.py:1248."
        )
    finally:
        counter_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await counter_task


@pytest.mark.asyncio
async def test_stream_slide_pumps_frames_to_renderer(renderer, tmp_path, monkeypatch):
    """A StreamSlide in the playlist is intercepted before the IPC
    path; its (mock) stream frames are pushed straight to the renderer.

    HW-decode (2026-05-20): the consumer emits source-resolution NV12
    (8x8 -> 96-byte NV12 frames here), and the pump threads the NV12
    pixel_format + source dims into render_frame()."""
    # NV12 frame size for the injected 8x8 source.
    frame_size = 8 * 8 * 3 // 2
    mock = _write_mock_ffmpeg(tmp_path / "ffmpeg", frame_size=frame_size, n_frames=5)
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
    slide = StreamSlide(name="live", stream_url="rtsp://h:8554/x", duration_ms=2000)
    loop = _new_loop(renderer, fetch_items=lambda: [slide], read_asset=lambda _id: b"")
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
async def test_stream_unreachable_skip_advances_immediately(renderer, tmp_path, monkeypatch):
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
async def test_stream_unreachable_hold_waits_out_the_slot(renderer, tmp_path, monkeypatch):
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
async def test_stream_unreachable_black_paints_a_black_frame(renderer, tmp_path, monkeypatch):
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
    loop = _new_loop(renderer, fetch_items=lambda: [stream], read_asset=lambda _id: b"")
    await loop.start()
    await asyncio.sleep(0.2)
    await loop.stop()
    assert bytes(8 * 8 * 3) in captured


@pytest.mark.asyncio
async def test_stream_connect_timeout_falls_back(renderer, tmp_path, monkeypatch):
    """If ffmpeg spawns but delivers no frame within the connect
    timeout, the slide falls back to on_unreachable rather than
    blocking on the dead stream for the whole slot."""
    monkeypatch.setattr("openmarquee.playback._STREAM_CONNECT_TIMEOUT_S", 0.2)
    # hang mock: spawns, emits 0 frames, then sleeps — ffmpeg is up but
    # never produces video, exactly how an unreachable stream URL behaves.
    mock = _write_mock_ffmpeg(tmp_path / "ffmpeg", frame_size=8 * 8 * 3, n_frames=0, hang=True)
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
async def test_stream_slide_preempted_by_pause(renderer, tmp_path, monkeypatch):
    """A pause() during a StreamSlide slot is honored — the loop
    yields the renderer and saves the resume index, so a stream
    takeover can preempt a stream slot."""
    mock = _write_mock_ffmpeg(
        tmp_path / "ffmpeg", frame_size=8 * 8 * 3, n_frames=0, continuous=True
    )
    _patch_stream_ffmpeg(monkeypatch, mock)
    stream = StreamSlide(name="live", stream_url="rtsp://h/x", duration_ms=10_000)
    loop = _new_loop(renderer, fetch_items=lambda: [stream], read_asset=lambda _id: b"")
    await loop.start()
    await asyncio.sleep(0.15)  # let the pump start streaming frames
    await loop.pause()
    # pause() only sets _pause_event — the playback loop must yield
    # from its current op, see the event, and commit _resume_at_index.
    # Under batch-test load (1300+ tests ahead of this one) a fixed
    # sleep races that takeover, so poll the actual condition with a
    # 2s ceiling.
    loop_t = asyncio.get_event_loop()
    deadline = loop_t.time() + 2.0
    while loop_t.time() < deadline and loop._resume_at_index is None:
        await asyncio.sleep(0.01)
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
    assert not web_refresh_due(100.0, now_monotonic=250.0, refresh_interval_s=300)


def test_web_refresh_due_stale_slide_is_due():
    """A slide whose last fetch is older than refresh_interval_s IS due."""
    # Fetched at t=100, interval 300s, now t=500 -> 400s elapsed >= 300.
    assert web_refresh_due(100.0, now_monotonic=500.0, refresh_interval_s=300)


def test_web_refresh_due_exactly_at_interval_is_due():
    """Elapsed exactly equal to the interval counts as due."""
    assert web_refresh_due(100.0, now_monotonic=400.0, refresh_interval_s=300)


@pytest.mark.asyncio
async def test_web_slide_kicks_refresh_producer(renderer):
    """Entering a Web slide's slot fires the screenshot producer."""
    web = WebSlide(
        name="status",
        url="https://h/x",
        duration_ms=_FAST_DURATION_MS,
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
        name="status",
        url="https://h/x",
        duration_ms=_FAST_DURATION_MS,
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
        name="status",
        url="https://h/x",
        duration_ms=_FAST_DURATION_MS,
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
        name="status",
        url="https://h/x",
        duration_ms=_FAST_DURATION_MS,
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
        name="a",
        url="https://h/a",
        duration_ms=_FAST_DURATION_MS,
        refresh_interval_s=10,
    )
    web_b = WebSlide(
        name="b",
        url="https://h/b",
        duration_ms=_FAST_DURATION_MS,
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
async def test_prune_web_tracking_drops_entries_older_than_24h(renderer):
    """r12 (2026-05-26) memory audit: _prune_web_tracking now ALSO
    drops entries older than 24h, even if the slide is still in the
    playlist. The original playlist-only prune leaks under playlist
    churn — entries persist across different playlists keyed by the
    same slide id. With the time-based prune, the dict is bounded
    to a 24h sliding window regardless.

    Pin the contract: an entry with a timestamp > 24h ago is
    removed by _prune_web_tracking even when its slide id IS in
    the current playlist.

    Test is async so `asyncio.get_event_loop()` reads the running
    loop (Python 3.11+: no deprecation; 3.14+ future-safe). The
    production prune at playback.py:_prune_web_tracking uses the
    same `asyncio.get_event_loop().time()` clock; both ends agree."""
    web = WebSlide(
        name="long-lived",
        url="https://h/x",
        duration_ms=_FAST_DURATION_MS,
        refresh_interval_s=10,
    )
    loop = _new_loop(
        renderer,
        fetch_items=lambda: [web],
        read_asset=lambda _id: _png_bytes(8, 8, (1, 2, 3)),
    )
    # Seed the tracking dict directly with a stale timestamp (25h
    # ago). asyncio.get_event_loop().time() returns the running
    # event-loop's monotonic clock — the same clock production's
    # prune reads.
    now = asyncio.get_event_loop().time()
    loop._web_last_fetch[web.id] = now - 25 * 3600  # 25h ago

    # Slide is still in the playlist — the playlist-only prune
    # would KEEP this entry. The time-based prune drops it.
    loop._prune_web_tracking([web])

    assert web.id not in loop._web_last_fetch, (
        "_prune_web_tracking should drop entries older than 24h even "
        "when the slide is still in the playlist (r12 time-based prune)"
    )


@pytest.mark.asyncio
async def test_inflight_id_not_pruned_when_slide_leaves_playlist(renderer):
    """C3/M1: a Web slide id whose fetch is still IN FLIGHT must NOT be
    pruned from _web_inflight even after it leaves the playlist —
    pruning a genuinely-running id would re-enable a double-kick. The
    set self-cleans via the kick's done-callback when the task ends."""
    web = WebSlide(
        name="status",
        url="https://h/x",
        duration_ms=_FAST_DURATION_MS,
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
        name="status",
        url="https://h/x",
        duration_ms=_FAST_DURATION_MS,
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
        name="status",
        url="https://h/x",
        duration_ms=_FAST_DURATION_MS,
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


# ---- _wait equivalence tests (QA perf P2 prep, 2026-05-24) ----
#
# PlaybackLoop._wait sleeps up to `seconds`, returning early on stop
# or pause. Originally implemented by racing two asyncio.create_task
# wrappers via asyncio.wait(); per QA's perf-resweep v2 P1 finding,
# the per-tick 2-Task allocation cost ~0.45% of one core. These tests
# pin the observable contract (sleep cadence, early-wake on stop, early-
# wake on pause, race-on-entry, zero-second short-circuit, cancellation
# propagation) so a refactor can be verified behaviorally-equivalent.


def _wait_test_loop(renderer):
    """Construct a PlaybackLoop and manually initialize the events
    that start() would normally create. This lets us exercise _wait
    in isolation without spinning the full loop body."""
    loop = PlaybackLoop(renderer, fetch_items=lambda: [], read_asset=lambda _i: b"")
    loop._stop_event = asyncio.Event()
    loop._pause_event = asyncio.Event()
    loop._resume_event = asyncio.Event()
    loop._wake_event = asyncio.Event()
    loop._resume_event.set()
    return loop


def _fake_stop(loop):
    """Model what production stop() does to the events that _wait
    races against. We can't call the real stop() in these tests
    because it short-circuits on `not is_running` (we never started
    the loop's task)."""
    loop._stop_event.set()
    loop._wake_event.set()


def _fake_pause(loop):
    """Model what production pause() does to the events that _wait
    races against. Same `not is_running` short-circuit reason as
    `_fake_stop`."""
    loop._pause_event.set()
    loop._wake_event.set()


@pytest.mark.asyncio
async def test_wait_sleeps_full_duration_when_no_signal(renderer):
    """Neither stop nor pause is set → _wait sleeps for ~seconds.
    Allow a generous tolerance (50%) so a slow CI runner doesn't
    flake; the contract is "approximately the requested duration",
    not "to the millisecond"."""
    loop = _wait_test_loop(renderer)
    t0 = asyncio.get_running_loop().time()
    await loop._wait(0.10)
    elapsed = asyncio.get_running_loop().time() - t0
    assert 0.08 <= elapsed <= 0.20, f"_wait(0.10) took {elapsed:.3f}s; expected ~0.10s ± tolerance"


@pytest.mark.asyncio
async def test_wait_returns_immediately_when_stop_set_during_wait(renderer):
    """stop() fires mid-wait → _wait returns shortly after the set,
    not after the full timeout. The pre-set asyncio scheduling jitter
    is bounded by the event loop's tick."""
    loop = _wait_test_loop(renderer)

    async def set_stop_after(delay):
        await asyncio.sleep(delay)
        _fake_stop(loop)

    t0 = asyncio.get_running_loop().time()
    waiter = asyncio.create_task(loop._wait(1.0))
    setter = asyncio.create_task(set_stop_after(0.05))
    await asyncio.gather(waiter, setter)
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed < 0.20, f"_wait(1.0) with mid-wait stop took {elapsed:.3f}s; expected <0.20s"


@pytest.mark.asyncio
async def test_wait_returns_immediately_when_pause_set_during_wait(renderer):
    """pause() fires mid-wait → _wait returns shortly after the set.
    Same shape as the stop case. Live-takeover responsiveness contract:
    when an operator hits Take Over, the playlist's current sleep must
    wake quickly rather than draining the full slide duration."""
    loop = _wait_test_loop(renderer)

    async def set_pause_after(delay):
        await asyncio.sleep(delay)
        _fake_pause(loop)

    t0 = asyncio.get_running_loop().time()
    waiter = asyncio.create_task(loop._wait(1.0))
    setter = asyncio.create_task(set_pause_after(0.05))
    await asyncio.gather(waiter, setter)
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed < 0.20, f"_wait(1.0) with mid-wait pause took {elapsed:.3f}s; expected <0.20s"


@pytest.mark.asyncio
async def test_wait_returns_immediately_when_stop_set_before_call(renderer):
    """stop() was set BEFORE _wait was invoked → _wait must short-
    circuit at entry, not sleep for the full duration. Race-on-entry
    contract: a stop racing with a slide-end transition should not
    introduce up-to-`seconds` of latency."""
    loop = _wait_test_loop(renderer)
    loop._stop_event.set()
    t0 = asyncio.get_running_loop().time()
    await loop._wait(1.0)
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed < 0.05, f"_wait(1.0) with pre-set stop took {elapsed:.3f}s; expected <0.05s"


@pytest.mark.asyncio
async def test_wait_returns_immediately_when_pause_set_before_call(renderer):
    """pause() was set BEFORE _wait was invoked → same race-on-entry
    short-circuit as stop. Without this, an operator-pause racing
    with a slide-end transition would still incur up to `seconds`
    of playlist render."""
    loop = _wait_test_loop(renderer)
    loop._pause_event.set()
    t0 = asyncio.get_running_loop().time()
    await loop._wait(1.0)
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed < 0.05, f"_wait(1.0) with pre-set pause took {elapsed:.3f}s; expected <0.05s"


@pytest.mark.asyncio
async def test_wait_with_zero_seconds_returns_quickly(renderer):
    """_wait(0) is occasionally invoked as a yield-to-event-loop with
    no actual sleep. Must return promptly (within asyncio scheduler
    jitter) rather than blocking indefinitely."""
    loop = _wait_test_loop(renderer)
    t0 = asyncio.get_running_loop().time()
    await loop._wait(0)
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed < 0.05, f"_wait(0) took {elapsed:.3f}s; expected <0.05s"


@pytest.mark.asyncio
async def test_wait_propagates_cancellation(renderer):
    """If the awaiting task is cancelled (e.g. the outer loop is
    teardown'd via stop() → task.cancel() from outside), the cancel
    must propagate through _wait — not be swallowed by the internal
    suppress(CancelledError, Exception) cleanup. Otherwise a teardown
    could hang waiting for _wait to drain its full timeout."""
    loop = _wait_test_loop(renderer)

    waiter = asyncio.create_task(loop._wait(5.0))
    # Yield once so waiter actually enters the await inside _wait.
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter


@pytest.mark.asyncio
async def test_begin_transition_unsupported_slide_blames_next_not_current(renderer, caplog):
    """Round-23 correctness regression: when the Rust sidecar raises
    RustRendererUnsupportedSlideError from begin_transition (because
    the NEXT slide is a kind it can't paint), pre-fix the error
    escaped to _loop's broad except which:
      - logged ERROR naming the CURRENT slide (which played fine)
      - added the CURRENT slide id to _failed_slide_ids throttle set
      - throttled later real failures of the (innocent) current slide

    The throttle set pollution is the worst part -- a later actual
    failure on the current slide drops to DEBUG and is invisible in
    the journal.

    Post-fix: blame next_item.id, throttle next_item.id in
    _skipped_slide_ids (NOT _failed_slide_ids), log INFO not ERROR
    (this isn't a current-slide failure), return True so the current
    slide completes cleanly.

    Test: 2-slide playlist with current set to fade-transition into
    next; monkeypatch renderer.begin_transition to raise
    UnsupportedSlide when called with next.id. Run loop long enough
    to fire begin_transition. Assert:
      - next.id is in loop._skipped_slide_ids
      - current.id is NOT in loop._failed_slide_ids (innocent)
      - log line names next.id (not current.id)
    """
    import logging

    from openmarquee.rendering.rust_renderer import (
        RustRendererUnsupportedSlideError,
    )

    current_slide, current_png = _make_slide("current-A", (10, 20, 30))
    next_slide, next_png = _make_slide("next-B-unsupported", (40, 50, 60))
    # Force current to fade-transition into next so begin_transition
    # actually fires. Short duration_ms so the transition is reached
    # quickly within the test's sleep budget.
    current_slide = current_slide.model_copy(
        update={
            "transition": "fade",
            "transition_ms": 200,
            "duration_ms": 100,
        }
    )
    assets = {current_slide.id: current_png, next_slide.id: next_png}

    original_begin_transition = renderer.begin_transition

    def patched_begin_transition(to_slide_id, to_duration_ms, kind, transition_ms, t0_ms):
        if to_slide_id == next_slide.id:
            raise RustRendererUnsupportedSlideError(
                "simulated: next slide kind unsupported by sidecar"
            )
        return original_begin_transition(to_slide_id, to_duration_ms, kind, transition_ms, t0_ms)

    renderer.begin_transition = patched_begin_transition  # type: ignore[method-assign]

    loop = _new_loop(
        renderer,
        fetch_items=lambda: [current_slide, next_slide],
        read_asset=lambda i: assets[i],
    )

    with caplog.at_level(logging.INFO, logger="openmarquee.playback"):
        await loop.start()
        # current plays (100ms), then begin_transition for next fires
        # and raises. Allow a generous budget for the loop to hit that
        # path at least once.
        await asyncio.sleep(0.5)
        await loop.stop()

    # CRITICAL ASSERTION 1: next_slide.id was added to the
    # _skipped_slide_ids throttle set (not _failed_slide_ids).
    assert next_slide.id in loop._skipped_slide_ids, (
        f"next slide id must be added to _skipped_slide_ids "
        f"(skipped-kind throttle); got skipped={loop._skipped_slide_ids}"
    )

    # CRITICAL ASSERTION 2: current_slide.id was NOT polluted into
    # _failed_slide_ids. Pre-fix this set would contain current.id
    # because the uncaught error landed at _loop's broad except and
    # named the current item (the wrong slide).
    assert current_slide.id not in loop._failed_slide_ids, (
        f"current slide id must NOT be in _failed_slide_ids "
        f"(it played fine; the issue was the NEXT slide). "
        f"Got failed={loop._failed_slide_ids}"
    )

    # CRITICAL ASSERTION 3: log line names next.id, not current.id.
    # Pre-fix the journal would say "IPC playback failed for slide
    # id=<current>" which mis-attributed the failure.
    relevant_msgs = [
        r.getMessage()
        for r in caplog.records
        if "begin_transition" in r.getMessage().lower() or "unsupported" in r.getMessage().lower()
    ]
    assert any(str(next_slide.id) in msg for msg in relevant_msgs), (
        f"log must mention next slide id {next_slide.id}; got messages: {relevant_msgs}"
    )
    # Defensive: the current slide's id must NOT appear in any of
    # the begin_transition / unsupported log lines (would imply
    # pre-fix attribution leaked through).
    assert not any(
        str(current_slide.id) in msg and "begin_transition" in msg.lower() for msg in relevant_msgs
    ), (
        f"current slide id must NOT appear in begin_transition log "
        f"lines (the issue was the NEXT slide). Got: {relevant_msgs}"
    )


# Perf-night r3 (2026-05-26): playback-loop tick-budget bookkeeping.
# The ring buffer + percentile math + rate-limited warn behavior are
# the operator-facing signal the QA r3 dispatch asks for.


def test_loop_stats_empty_ring_returns_all_zeros(renderer):
    """Fresh PlaybackLoop instance — no tick has been recorded yet.
    get_loop_stats must return a clean all-zero shape (NOT raise) so
    the /api/playback/loop_stats endpoint can serve 200 OK at boot."""
    loop = _new_loop(
        renderer,
        fetch_items=lambda: [],
        read_asset=lambda _id: _png_bytes(8, 8, (0, 0, 0)),
    )
    stats = loop.get_loop_stats()
    assert stats == {
        "ticks_observed": 0,
        "p50_us": 0,
        "p95_us": 0,
        "p99_us": 0,
        "max_us": 0,
        "ticks_over_budget": 0,
    }


def test_loop_stats_percentile_math_matches_renderer_profile_rs(renderer):
    """Match the percentile-index convention from renderer/src/
    profile.rs:summarize_samples (min(int(n*pct/100), n-1)).

    With 100 samples of 1000us each, p50=p95=p99=max=1000us. With a
    single spike at the top, p99 catches the spike — we want the
    operator to see "the 99th-percentile tick was slow."
    """
    loop = _new_loop(
        renderer,
        fetch_items=lambda: [],
        read_asset=lambda _id: _png_bytes(8, 8, (0, 0, 0)),
    )
    slide_id = uuid4()
    # 99 fast ticks @ 1ms + 1 spike @ 50ms.
    for _ in range(99):
        loop._record_tick(1_000_000, slide_id, "advance")
    loop._record_tick(50_000_000, slide_id, "advance")
    stats = loop.get_loop_stats()
    assert stats["ticks_observed"] == 100
    assert stats["p50_us"] == 1000  # median is in the fast tier
    assert stats["p95_us"] == 1000  # 95th still in fast tier (95 < 99)
    assert stats["p99_us"] == 50_000  # 99th hits the spike
    assert stats["max_us"] == 50_000
    assert stats["ticks_over_budget"] == 1  # 50ms > 33ms threshold


def test_loop_stats_ring_evicts_oldest_at_600(renderer):
    """deque(maxlen=600) is the no-allocation eviction policy. Verify
    the ring stays bounded under heavy ingestion — a multi-day
    soak can't grow it without bound."""
    loop = _new_loop(
        renderer,
        fetch_items=lambda: [],
        read_asset=lambda _id: _png_bytes(8, 8, (0, 0, 0)),
    )
    slide_id = uuid4()
    # 1000 distinct ticks; only the last 600 should remain.
    for i in range(1000):
        loop._record_tick((i + 1) * 1_000, slide_id, "advance")
    stats = loop.get_loop_stats()
    assert stats["ticks_observed"] == 600
    # First-401 evicted; max = the last tick (1000us * 1000 = 1_000_000ns = 1000us)
    assert stats["max_us"] == 1000


def test_record_tick_under_budget_does_not_warn(renderer, caplog):
    """Sub-budget ticks (< 33ms) must NOT emit the warn log. Otherwise
    the journal floods at 30 ticks/sec under normal operation."""
    loop = _new_loop(
        renderer,
        fetch_items=lambda: [],
        read_asset=lambda _id: _png_bytes(8, 8, (0, 0, 0)),
    )
    slide_id = uuid4()
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="openmarquee.playback"):
        for _ in range(100):
            loop._record_tick(5_000_000, slide_id, "advance")  # 5ms — well under
    # Filter to just the playback logger so other loggers don't trip the assert.
    warns = [
        r for r in caplog.records if r.name == "openmarquee.playback" and r.levelname == "WARNING"
    ]
    assert warns == []


def test_record_tick_over_budget_warns_once_then_rate_limits(renderer, caplog, monkeypatch):
    """Multiple over-budget ticks in rapid succession emit ONE warn
    (then suppressed for 5s). The dispatch's hard rule on rate-limiting
    so a wedged readline doesn't spam logs."""
    loop = _new_loop(
        renderer,
        fetch_items=lambda: [],
        read_asset=lambda _id: _png_bytes(8, 8, (0, 0, 0)),
    )
    slide_id = uuid4()
    # Pin time.monotonic so the rate-limit math is deterministic.
    # Both calls fall in the same monotonic second → second warn
    # MUST be rate-limited.
    fixed_monotonic = [100.0]
    import time as _time

    monkeypatch.setattr(_time, "monotonic", lambda: fixed_monotonic[0])
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="openmarquee.playback"):
        loop._record_tick(50_000_000, slide_id, "advance")  # 50ms — over
        loop._record_tick(60_000_000, slide_id, "advance")  # 60ms — over
        loop._record_tick(70_000_000, slide_id, "advance")  # 70ms — over
    warns = [
        r for r in caplog.records if r.name == "openmarquee.playback" and r.levelname == "WARNING"
    ]
    assert len(warns) == 1
    assert "tick over budget" in warns[0].message
    assert "50.0ms" in warns[0].message  # first over-budget value
    # All 3 still recorded in the ring + counted as over_budget.
    stats = loop.get_loop_stats()
    assert stats["ticks_observed"] == 3
    assert stats["ticks_over_budget"] == 3


def test_record_tick_warn_fires_again_after_5s_window(renderer, caplog, monkeypatch):
    """After the 5s rate-limit window elapses, the NEXT over-budget
    tick warns again — operator gets fresh signal if the stutter
    recurs, not just the first incident."""
    loop = _new_loop(
        renderer,
        fetch_items=lambda: [],
        read_asset=lambda _id: _png_bytes(8, 8, (0, 0, 0)),
    )
    slide_id = uuid4()
    fixed_monotonic = [100.0]
    import time as _time

    monkeypatch.setattr(_time, "monotonic", lambda: fixed_monotonic[0])
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="openmarquee.playback"):
        loop._record_tick(50_000_000, slide_id, "advance")  # first warn
        fixed_monotonic[0] = 100.0 + 5.5  # advance past rate-limit gate
        loop._record_tick(50_000_000, slide_id, "advance")  # second warn
    warns = [
        r for r in caplog.records if r.name == "openmarquee.playback" and r.levelname == "WARNING"
    ]
    assert len(warns) == 2


# Perf-night r4 (2026-05-26): load-bearing invariant test for the
# asyncio.to_thread wrap on _renderer.advance / begin_slide /
# begin_transition. The fix runs the sync IPC body on an executor
# worker so the asyncio event loop stays responsive — other coroutines
# (FastAPI handlers, capture path, timer fires) progress while the
# IPC round-trip is in flight. The pattern test here proves that
# property cleanly: pre-fix, a bare sync call would wedge the loop
# and the counter coroutine would NOT increment; post-fix it does.


@pytest.mark.asyncio
async def test_advance_off_loop_allows_concurrent_progress():
    """The load-bearing r4 invariant. Pre-fix shape:

      result = self._renderer.advance(t_ms)  # sync; wedges loop

    Post-fix:

      result = await asyncio.to_thread(self._renderer.advance, t_ms)

    Spawn TWO concurrent coroutines:
      (1) one that does `await asyncio.to_thread(sleepy_advance, ...)`
          where `sleepy_advance` is a SYNC function that time.sleeps
          200ms (simulating the renderer's slow readline)
      (2) a counter coroutine that increments every 10ms

    Assert the counter incremented MANY times during the 200ms sleep.
    Pre-fix bare call → counter == 0 (loop wedged). Post-fix → counter
    > 5 (event loop kept running). The test proves the to_thread call
    actually releases the loop, not just looks-right at a glance."""
    import time as _time

    advance_done = []

    def sleepy_advance(t_ms: int) -> dict:
        """Sync function impersonating the renderer's blocking readline.
        time.sleep is the sync-I/O analog of subprocess.stdout.readline
        when the renderer is mid-paint."""
        _time.sleep(0.2)
        advance_done.append(t_ms)
        return {"command": "paint_slide", "t_in_slide_ms": t_ms}

    counter = 0

    async def tick_counter():
        """Coroutine that ONLY makes progress if the event loop is
        cooperative-scheduling. Pre-fix it would never increment past 0
        because the bare sleepy_advance() would block all other tasks."""
        nonlocal counter
        try:
            while True:
                await asyncio.sleep(0.01)
                counter += 1
        except asyncio.CancelledError:
            return

    counter_task = asyncio.create_task(tick_counter())
    try:
        result = await asyncio.to_thread(sleepy_advance, 1234)
        assert result["t_in_slide_ms"] == 1234
        assert advance_done == [1234]
        # 200ms sleep at 10ms increments => ~20 ticks. Lower bound 5
        # is conservative to handle scheduling jitter on slow CI
        # runners; what we actually want to verify is "counter is
        # measurably non-zero" (proves the loop wasn't wedged).
        assert counter > 5, (
            f"counter only reached {counter} — event loop appears wedged. "
            "asyncio.to_thread did NOT release the loop as expected."
        )
    finally:
        counter_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await counter_task


@pytest.mark.asyncio
async def test_playback_loop_advance_off_loop_in_full_run(renderer):
    """Integration flavor of the same invariant against the real
    `_play_via_rust_ipc` flow + load-bearing against future revert.

    Monkey-patches the MockRenderer's `advance` to inject a 30ms
    `time.sleep` per call — that simulates the slow renderer readline
    we're trying to keep off the asyncio loop. With the r4 wrap in
    place, each per-tick advance runs on an executor worker; the
    asyncio loop schedules the counter coroutine during the worker's
    sleep, so the counter ticks consistently.

    Pre-fix (bare `self._renderer.advance(t_ms)`), the 30ms sleep
    would block the asyncio loop entirely per tick — the counter
    coroutine couldn't run during sleeps, and at a 5ms cadence with
    multiple back-to-back wedged ticks the counter would stall hard.

    `counter > 5` is the post-fix floor on a normal CI runner; a
    revert of the asyncio.to_thread wrap would drop the counter to
    near-zero. This is the regression lock the soft `>= 1` previous
    assertion lacked."""
    import time as _time

    counter = 0

    async def tick_counter():
        nonlocal counter
        try:
            while True:
                await asyncio.sleep(0.005)
                counter += 1
        except asyncio.CancelledError:
            return

    slide, png = _make_slide("solo", (33, 66, 99))
    loop = _new_loop(
        renderer,
        fetch_items=lambda: [slide],
        read_asset=lambda _id: png,
    )

    # Inject the wedge simulator. functools.partial bound to a closure
    # over `_time.sleep` (NOT monkeypatch on the time module, so other
    # coroutines using asyncio.sleep are unaffected) — only the
    # specific renderer instance's advance does the sync block.
    real_advance = renderer.advance

    def slow_advance(t_ms):
        _time.sleep(0.03)  # 30ms sync wedge per call
        return real_advance(t_ms)

    renderer.advance = slow_advance

    counter_task = asyncio.create_task(tick_counter())
    try:
        await loop.start()
        # 100ms slide → ~3 ticks @ 30Hz; with 30ms wedge per tick the
        # advance loop spends ~90ms in worker-thread sleeps. Give the
        # outer scheduler enough time for the slide to finish and the
        # counter to accumulate.
        await asyncio.sleep(_FAST_DURATION_MS / 1000 + 0.1)
        await loop.stop()
        # Post-fix: counter increments freely during worker sleeps.
        # Floor of 5 = 25ms of accumulated counter time at 5ms cadence,
        # which is well below the ~90ms of total worker-sleep time the
        # scheduler had available. Pre-fix (bare sync call), the loop
        # would have been wedged for those ~90ms → counter near 0.
        assert counter > 5, (
            f"tick_counter only reached {counter} during the 90ms of "
            "renderer-sleep window — playback loop appears wedged. "
            "asyncio.to_thread wrap likely reverted or missing on the "
            "advance/begin_slide callsites."
        )
    finally:
        counter_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await counter_task


def test_record_tick_first_warn_fires_during_startup_window(renderer, caplog, monkeypatch):
    """Pre-edit, `self._last_tick_warn_at: float = 0.0` made the first
    over-budget tick suppress its warn whenever `time.monotonic() < 5.0`
    (5s rate-limit gate read against the zero sentinel). Process-
    startup stutters (cold cache, first IPC round-trip after a
    renderer respawn) live in that window — losing their warn was a
    real diagnostic gap.

    Post-edit: `_last_tick_warn_at: float | None = None`. The None
    sentinel is explicitly bypassed by the gate, so the first over-
    budget tick at startup ALWAYS warns. Locks against the
    pre-edit regression."""
    loop = _new_loop(
        renderer,
        fetch_items=lambda: [],
        read_asset=lambda _id: _png_bytes(8, 8, (0, 0, 0)),
    )
    slide_id = uuid4()
    # Pin monotonic to 1.5 — well inside the 5s startup window. Pre-
    # edit, gate evaluated to `1.5 - 0.0 < 5.0` → True → return →
    # warn suppressed. Post-edit, gate sees None sentinel → fires.
    import time as _time

    monkeypatch.setattr(_time, "monotonic", lambda: 1.5)
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="openmarquee.playback"):
        loop._record_tick(50_000_000, slide_id, "advance")
    warns = [
        r for r in caplog.records if r.name == "openmarquee.playback" and r.levelname == "WARNING"
    ]
    assert len(warns) == 1
    assert "tick over budget" in warns[0].message


# ============================================================
# r98 (2026-06-09): OPENMARQUEE_PRELOAD_MODE + OPENMARQUEE_PRELOAD_LEAD_MS
# env-var parsing.
# ============================================================


class TestResolvePreloadMode:
    def test_default_when_unset(self):
        from openmarquee.playback import _resolve_preload_mode

        assert _resolve_preload_mode(env={}) == "defer"

    def test_recognises_canonical_modes(self):
        from openmarquee.playback import _resolve_preload_mode

        assert _resolve_preload_mode(env={"OPENMARQUEE_PRELOAD_MODE": "defer"}) == "defer"
        assert _resolve_preload_mode(env={"OPENMARQUEE_PRELOAD_MODE": "lead"}) == "lead"
        assert _resolve_preload_mode(env={"OPENMARQUEE_PRELOAD_MODE": "max"}) == "max"

    def test_case_insensitive(self):
        from openmarquee.playback import _resolve_preload_mode

        for raw in ["DEFER", "Defer", "DeFeR"]:
            assert _resolve_preload_mode(env={"OPENMARQUEE_PRELOAD_MODE": raw}) == "defer"
        for raw in ["LEAD", "Lead", "lEaD"]:
            assert _resolve_preload_mode(env={"OPENMARQUEE_PRELOAD_MODE": raw}) == "lead"
        for raw in ["MAX", "Max", "mAx"]:
            assert _resolve_preload_mode(env={"OPENMARQUEE_PRELOAD_MODE": raw}) == "max"

    def test_whitespace_around_value_normalised(self):
        from openmarquee.playback import _resolve_preload_mode

        assert _resolve_preload_mode(env={"OPENMARQUEE_PRELOAD_MODE": "  defer\n"}) == "defer"
        assert _resolve_preload_mode(env={"OPENMARQUEE_PRELOAD_MODE": " lead "}) == "lead"
        assert _resolve_preload_mode(env={"OPENMARQUEE_PRELOAD_MODE": "\tmax\t"}) == "max"

    def test_empty_string_silently_defaults(self, caplog):
        # Empty string is operator-typo-equivalent (unset drop-in),
        # not a real misconfiguration -- silently default, no warn.
        from openmarquee.playback import _resolve_preload_mode

        with caplog.at_level(logging.WARNING, logger="openmarquee.playback"):
            assert _resolve_preload_mode(env={"OPENMARQUEE_PRELOAD_MODE": ""}) == "defer"
        assert not any("OPENMARQUEE_PRELOAD_MODE" in r.message for r in caplog.records)

    def test_garbage_warns_and_defaults(self, caplog):
        # Unrecognised non-empty value should WARN (operator typo or
        # bad config) and fall back to defer for safety.
        from openmarquee.playback import _resolve_preload_mode

        with caplog.at_level(logging.WARNING, logger="openmarquee.playback"):
            assert _resolve_preload_mode(env={"OPENMARQUEE_PRELOAD_MODE": "deferred"}) == "defer"
            assert _resolve_preload_mode(env={"OPENMARQUEE_PRELOAD_MODE": "true"}) == "defer"
            assert _resolve_preload_mode(env={"OPENMARQUEE_PRELOAD_MODE": "maximum"}) == "defer"
        warn_messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
        # 3 distinct unrecognised values -> 3 warnings.
        assert sum(1 for m in warn_messages if "OPENMARQUEE_PRELOAD_MODE" in m) == 3

    # 2026-06-13 FYS-regression-class tests. The leftover MODE=max
    # drop-in starved the FROM-side bg decoder during every transition
    # (outgoing video went black; see docs/hardware-ceilings.md). The
    # tests below pin the experiment-knob's loud-at-runtime warning AND
    # ensure no shipped surface in this repo ever sets `max` literally.

    def test_max_emits_experiment_only_warning(self, caplog):
        # The resolver must emit a WARNING when the operator explicitly
        # opts into 'max' so the journal makes the experiment-knob's
        # presence loud at process startup.
        from openmarquee.playback import _resolve_preload_mode

        with caplog.at_level(logging.WARNING, logger="openmarquee.playback"):
            assert _resolve_preload_mode(env={"OPENMARQUEE_PRELOAD_MODE": "max"}) == "max"
        warn_messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("EXPERIMENT-ONLY" in m and "max" in m.lower() for m in warn_messages), (
            "expected an EXPERIMENT-ONLY warning naming the value when MODE=max "
            "is resolved; got: " + repr(warn_messages)
        )
        assert any("hardware-ceilings.md" in m for m in warn_messages), (
            "warning must cite the docs path so operators can find the contract"
        )

    def test_lead_emits_experiment_only_warning(self, caplog):
        # Mirror the max case for the 'lead' bench mode.
        from openmarquee.playback import _resolve_preload_mode

        with caplog.at_level(logging.WARNING, logger="openmarquee.playback"):
            assert _resolve_preload_mode(env={"OPENMARQUEE_PRELOAD_MODE": "lead"}) == "lead"
        warn_messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("EXPERIMENT-ONLY" in m and "lead" in m.lower() for m in warn_messages), (
            "expected an EXPERIMENT-ONLY warning naming the value when "
            "MODE=lead is resolved; got: " + repr(warn_messages)
        )

    def test_defer_does_not_emit_experiment_warning(self, caplog):
        # The production mode must NOT spam the journal with the
        # experiment warning. Both the explicit 'defer' and the unset
        # default path are pinned.
        from openmarquee.playback import _resolve_preload_mode

        with caplog.at_level(logging.WARNING, logger="openmarquee.playback"):
            assert _resolve_preload_mode(env={}) == "defer"
            assert _resolve_preload_mode(env={"OPENMARQUEE_PRELOAD_MODE": "defer"}) == "defer"
        warn_messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert not any("EXPERIMENT-ONLY" in m for m in warn_messages), (
            "defer mode is production; no EXPERIMENT-ONLY warning expected"
        )

    def test_repo_setter_lint_is_case_insensitive(self, tmp_path, monkeypatch):
        # Sacred-review BLOCKER-1: the lint must catch case variants
        # because BOTH consumers normalise case. A 'Environment=
        # OPENMARQUEE_PRELOAD_MODE=Max' drop-in in a future deploy
        # surface would activate the experiment knob identically to
        # 'max' but slip a case-sensitive regex. Pin the regex inside
        # the function so it can't drift away from the production
        # lint above.
        import re

        banned_re = re.compile(
            r"OPENMARQUEE_PRELOAD_MODE\s*=\s*[\"']?\s*(max|lead)\b",
            re.IGNORECASE,
        )
        # Must MATCH every case variant of max/lead.
        for variant in [
            "max",
            "MAX",
            "Max",
            "mAx",
            "MaX",
            "lead",
            "LEAD",
            "Lead",
            "lEaD",
            "LeAd",
        ]:
            assert banned_re.search(f'Environment="OPENMARQUEE_PRELOAD_MODE={variant}"'), (
                f"lint must catch case variant {variant!r}"
            )
        # Must NOT match production value or any other env.
        for clean in [
            'Environment="OPENMARQUEE_PRELOAD_MODE=defer"',
            "Environment=OPENMARQUEE_PRELOAD_MODE=DEFER",
            'Environment="SOMETHING_ELSE=max"',
            'Environment="OPENMARQUEE_PRELOAD_LEAD_MS=2000"',
        ]:
            assert not banned_re.search(clean), f"lint must NOT match clean line {clean!r}"

    def test_no_repo_setter_ships_preload_mode_max(self):
        # 2026-06-13 regression-lock: the bug that bit FYS was a
        # leftover Environment=OPENMARQUEE_PRELOAD_MODE=max drop-in
        # added during a dual-1080p experiment and never removed.
        # NOTHING in the repo's deploy surface (systemd units, install
        # scripts, deploy scripts, stage scripts) is allowed to set
        # MODE=max. The docs are exempt — they document the contract.
        import pathlib
        import re

        repo_root = pathlib.Path(__file__).resolve().parents[2]
        # Glob the deploy surface explicitly. Don't recurse into
        # target/, node_modules/, .git/, docs/ (docs DOCUMENT the
        # value verbatim — see hardware-ceilings.md), qa/ (audit logs
        # may quote prior incidents), backend/tests/ (this test plus
        # the others quote the literal string verbatim).
        # `system/**/*.service.d/**/*.conf` is fully covered by
        # `system/**/*.conf` (reviewer NIT-1: empirically dedup'd to
        # the same files). Kept narrow to the realistic deploy
        # surface — *.service / *.timer / *.conf for systemd,
        # *.sh / *.py for the install + deploy + stage scripts.
        deploy_globs = [
            "system/**/*.service",
            "system/**/*.conf",
            "system/**/*.timer",
            "system/**/*.sh",
            "scripts/**/*.sh",
            "scripts/**/*.py",
            "images/**/*.sh",
            "images/**/*.service",
            "images/**/*.conf",
        ]
        # The literal pattern we're banning. Tolerant of quoting
        # variants because systemd accepts both Environment="X=Y" and
        # Environment=X=Y, and shell scripts vary. Case-insensitive
        # because BOTH consumers normalise: Python `_resolve_preload_
        # mode` calls .lower() and Rust `parse_preload_mode` uses
        # eq_ignore_ascii_case — so a `Max` or `mAx` drop-in would
        # activate the experiment knob the same as `max` and the
        # lint must catch it.
        banned_re = re.compile(
            r"OPENMARQUEE_PRELOAD_MODE\s*=\s*[\"']?\s*(max|lead)\b",
            re.IGNORECASE,
        )
        offenders: list[str] = []
        for pattern in deploy_globs:
            for path in repo_root.glob(pattern):
                if not path.is_file():
                    continue
                try:
                    text = path.read_text()
                except (OSError, UnicodeDecodeError):
                    continue
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if banned_re.search(line):
                        rel = path.relative_to(repo_root)
                        offenders.append(f"{rel}:{lineno}: {line.strip()}")
        assert not offenders, (
            "Production deploy surface ships an experiment-only "
            "OPENMARQUEE_PRELOAD_MODE value. See docs/hardware-ceilings.md "
            "for the contract — production must be 'defer' (the code "
            "default). Offending lines:\n  " + "\n  ".join(offenders)
        )


class TestResolvePreloadLeadSeconds:
    def test_default_when_unset(self):
        from openmarquee.playback import _resolve_preload_lead_seconds

        assert _resolve_preload_lead_seconds(env={}) == 1.0

    def test_default_when_empty_string(self):
        from openmarquee.playback import _resolve_preload_lead_seconds

        assert _resolve_preload_lead_seconds(env={"OPENMARQUEE_PRELOAD_LEAD_MS": ""}) == 1.0

    def test_valid_in_range(self):
        from openmarquee.playback import _resolve_preload_lead_seconds

        assert _resolve_preload_lead_seconds(env={"OPENMARQUEE_PRELOAD_LEAD_MS": "500"}) == 0.5
        assert _resolve_preload_lead_seconds(env={"OPENMARQUEE_PRELOAD_LEAD_MS": "3000"}) == 3.0
        assert _resolve_preload_lead_seconds(env={"OPENMARQUEE_PRELOAD_LEAD_MS": "4000"}) == 4.0
        # Range boundaries.
        assert _resolve_preload_lead_seconds(env={"OPENMARQUEE_PRELOAD_LEAD_MS": "100"}) == 0.1
        assert _resolve_preload_lead_seconds(env={"OPENMARQUEE_PRELOAD_LEAD_MS": "10000"}) == 10.0

    def test_below_min_falls_back_with_warn(self, caplog):
        from openmarquee.playback import _resolve_preload_lead_seconds

        with caplog.at_level(logging.WARNING, logger="openmarquee.playback"):
            assert _resolve_preload_lead_seconds(env={"OPENMARQUEE_PRELOAD_LEAD_MS": "50"}) == 1.0
            assert _resolve_preload_lead_seconds(env={"OPENMARQUEE_PRELOAD_LEAD_MS": "0"}) == 1.0
            assert _resolve_preload_lead_seconds(env={"OPENMARQUEE_PRELOAD_LEAD_MS": "-1"}) == 1.0
        assert sum(1 for r in caplog.records if "OPENMARQUEE_PRELOAD_LEAD_MS" in r.message) == 3

    def test_above_max_falls_back_with_warn(self, caplog):
        from openmarquee.playback import _resolve_preload_lead_seconds

        with caplog.at_level(logging.WARNING, logger="openmarquee.playback"):
            assert (
                _resolve_preload_lead_seconds(env={"OPENMARQUEE_PRELOAD_LEAD_MS": "10001"}) == 1.0
            )
            assert (
                _resolve_preload_lead_seconds(env={"OPENMARQUEE_PRELOAD_LEAD_MS": "99999"}) == 1.0
            )
        assert sum(1 for r in caplog.records if "OPENMARQUEE_PRELOAD_LEAD_MS" in r.message) == 2

    def test_non_integer_falls_back_with_warn(self, caplog):
        from openmarquee.playback import _resolve_preload_lead_seconds

        with caplog.at_level(logging.WARNING, logger="openmarquee.playback"):
            assert _resolve_preload_lead_seconds(env={"OPENMARQUEE_PRELOAD_LEAD_MS": "3.14"}) == 1.0
            assert _resolve_preload_lead_seconds(env={"OPENMARQUEE_PRELOAD_LEAD_MS": "abc"}) == 1.0
            assert _resolve_preload_lead_seconds(env={"OPENMARQUEE_PRELOAD_LEAD_MS": "µs"}) == 1.0
        assert sum(1 for r in caplog.records if "OPENMARQUEE_PRELOAD_LEAD_MS" in r.message) == 3
