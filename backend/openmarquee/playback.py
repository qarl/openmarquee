"""Background playback loop that drives a renderer with content items.

The loop runs as an asyncio task. On each iteration it:

- fetches the current list of items (via an injected callable)
- if empty, sleeps briefly and rechecks
- otherwise advances through them in order, decoding each item's PNG
  to RGB and pushing it to the renderer for the item's duration

VideoSlide note: the loop treats videos as still thumbnails today —
`asset.png` is the first-frame thumbnail saved at upload time, so a
VideoSlide in the playlist shows the thumbnail for `duration_ms` and
advances. Real video playback (decoding asset.mp4 to frames on the Pi's
hardware H.264 decoder) lands with the HDMI renderer (Phase 6).

Items are re-fetched between iterations, so adding/deleting slides while
playing takes effect on the next cycle without restarting the loop. A
failed render (missing asset, corrupt PNG) is logged and skipped — one
bad slide doesn't kill the loop.

This obsoletes the manual `/dev/play/{id}` poke from Phase 2's dev
tooling. The dev preview now updates continuously while playback is
running, like a real sign.
"""

import asyncio
import contextlib
import io
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from PIL import Image, UnidentifiedImageError

from openmarquee.content import ContentItem
from openmarquee.rendering import Renderer

if TYPE_CHECKING:
    from openmarquee.content.storage import ContentStorage
    from openmarquee.playlist import PlaylistStorage
    from openmarquee.schedule import ScheduleStorage

log = logging.getLogger(__name__)

# Frame rate for fade transitions. 30fps is the playback target on both HDMI
# and HUB75; ~15 frames over a default 500ms fade is plenty smooth.
_FADE_FPS = 30


class PlaybackLoop:
    """Cycles content items through a renderer until told to stop.

    Designed for dependency-injected use — accepts a renderer and two
    plain callables (fetch_items, read_asset) so tests don't need to
    spin up real storage or a real renderer.
    """

    def __init__(
        self,
        renderer: Renderer,
        fetch_items: Callable[[], list[ContentItem]],
        read_asset: Callable[[UUID], bytes],
        empty_playlist_poll_seconds: float = 1.0,
    ):
        self._renderer = renderer
        self._fetch_items = fetch_items
        self._read_asset = read_asset
        self._empty_poll = empty_playlist_poll_seconds
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._current_id: UUID | None = None
        # Exposed alongside _current_id so the UI's live preview knows
        # whether to render a <video> or a <img> without a second round
        # trip to /api/content/{id}.
        self._current_type: str | None = None
        # Set by fetch_items if it carries playlist context (the
        # scheduled_fetch_items closure stamps this each fetch).
        self._current_playlist_name: str | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def current_item_id(self) -> UUID | None:
        return self._current_id

    @property
    def current_item_type(self) -> str | None:
        return self._current_type

    async def start(self) -> None:
        """Start the loop. No-op if already running."""
        if self.is_running:
            return
        # Bind the Event to the running event loop on each start.
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Signal the loop to stop and wait for it to exit. No-op if not running."""
        if not self.is_running:
            return
        assert self._stop_event is not None and self._task is not None
        # Null _task synchronously so an interleaved start() during the await
        # below sees `is_running == False` and starts a fresh loop instead of
        # silently no-op'ing.
        task = self._task
        self._task = None
        self._stop_event.set()
        try:
            await task
        finally:
            self._stop_event = None
            self._current_id = None
            self._current_type = None
            self._current_playlist_name = None

    async def _loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                items = self._fetch_items()
            except Exception:
                log.exception("playback: fetch_items failed")
                items = []

            if not items:
                self._current_id = None
                self._current_type = None
                await self._wait(self._empty_poll)
                continue

            # Pre-load images lazily inside the per-item iteration so a
            # mid-cycle add/delete is reflected on the next pass without
            # ballooning memory for a hundred-slide playlist.
            for i, item in enumerate(items):
                if self._stop_event.is_set():
                    break
                self._current_id = item.id
                self._current_type = item.type

                current_image = self._safe_load_image(item)
                if current_image is None:
                    continue

                self._render_image(current_image)
                await self._wait(item.duration_ms / 1000)

                if self._stop_event.is_set():
                    break

                # Transition into the next slide (or wrap to first). Skip
                # for cut (instant), single-item playlists (next == current),
                # and for the last item if we're about to re-enter the outer
                # loop and re-fetch (the next iteration's first item handles
                # its own appearance via cut/fade as configured).
                if item.transition == "fade" and item.transition_ms > 0 and len(items) > 1:
                    next_item = items[(i + 1) % len(items)]
                    next_image = self._safe_load_image(next_item)
                    if next_image is not None:
                        await self._fade(current_image, next_image, item.transition_ms)

    async def _wait(self, seconds: float) -> None:
        """Sleep up to `seconds`, returning early if stop is requested."""
        assert self._stop_event is not None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)

    def _safe_load_image(self, item: ContentItem) -> Image.Image | None:
        """Load + resize an item's PNG to renderer dimensions.

        Returns None on missing asset / corrupt PNG / any other render-time
        failure — playback continues with the next item.
        """
        try:
            asset_bytes = self._read_asset(item.id)
        except FileNotFoundError:
            log.warning("playback: asset missing for %s, skipping", item.id)
            return None
        except Exception:
            log.exception("playback: failed to read asset for %s", item.id)
            return None

        try:
            image = Image.open(io.BytesIO(asset_bytes)).convert("RGB")
        except UnidentifiedImageError:
            log.warning("playback: corrupt asset for %s, skipping", item.id)
            return None

        if image.size != (self._renderer.width, self._renderer.height):
            image = image.resize(
                (self._renderer.width, self._renderer.height),
                resample=Image.Resampling.NEAREST,
            )
        return image

    def _render_image(self, image: Image.Image) -> None:
        """Push an already-loaded, correctly-sized image to the renderer.

        Wrapped in try/except so a renderer crash doesn't kill the loop —
        same survival contract _safe_render had.
        """
        try:
            self._renderer.render_frame(image.tobytes())
        except Exception:
            log.exception("playback: renderer raised on render_frame")

    @property
    def current_playlist_name(self) -> str | None:
        """The playlist name the loop is currently sourcing items from.

        Set by the schedule-driven fetch fn at the start of each iteration so
        the UI can show "now playing from <playlist>". None when not running.
        """
        return self._current_playlist_name

    def _stamp_playlist_name(self, name: str | None) -> None:
        """Hook for the scheduled fetch fn to publish which playlist is
        currently active. Test-only setter is just self._current_playlist_name."""
        self._current_playlist_name = name

    async def _fade(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Alpha-blend from `from_image` to `to_image` over `transition_ms`.

        Returns early if stop is requested mid-fade. Image.blend is the actual
        per-pixel math — at alpha=0 we get from_image; at alpha=1 we get
        to_image.
        """
        n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
        frame_period = (transition_ms / 1000) / n_frames
        for i in range(1, n_frames + 1):
            assert self._stop_event is not None
            if self._stop_event.is_set():
                return
            alpha = i / n_frames
            blended = Image.blend(from_image, to_image, alpha)
            self._render_image(blended)
            await self._wait(frame_period)


def scheduled_fetch_items(
    content_storage: "ContentStorage",
    playlist_storage: "PlaylistStorage",
    schedule_storage: "ScheduleStorage",
    now: datetime,
    loop: PlaybackLoop | None = None,
) -> list[ContentItem]:
    """Return items in the order of the playlist active per the schedule at `now`.

    Deferred imports inside the function dodge a content↔playlist↔schedule
    circular at module load. The composition is small enough to inline; pulling
    it into a separate "wiring" module would just hide it.

    If `loop` is provided, stamps `_current_playlist_name` so the UI can show
    which playlist is active. The PlaybackLoop's fetch_items closure passes
    itself in via `partial`.
    """
    from openmarquee.playlist import list_in_playlist_order
    from openmarquee.schedule import evaluate_schedule

    schedule = schedule_storage.load()
    active_name = evaluate_schedule(now, schedule)
    if loop is not None:
        loop._stamp_playlist_name(active_name)
    return list_in_playlist_order(content_storage, playlist_storage, active_name)
