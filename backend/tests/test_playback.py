import asyncio
import io
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


def _new_loop(renderer, *, fetch_items, read_asset):
    return PlaybackLoop(
        renderer,
        fetch_items=fetch_items,
        read_asset=read_asset,
        empty_playlist_poll_seconds=_FAST_EMPTY_POLL,
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
