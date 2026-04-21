"""Background playback loop that drives a renderer with content items.

The loop runs as an asyncio task. On each iteration it:

- fetches the current list of items (via an injected callable)
- if empty, sleeps briefly and rechecks
- otherwise advances through them in order, decoding each item's PNG
  to RGB and pushing it to the renderer for the item's duration

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
from uuid import UUID

from PIL import Image, UnidentifiedImageError

from openmarquee.content import ContentItem
from openmarquee.rendering import Renderer

log = logging.getLogger(__name__)


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

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def current_item_id(self) -> UUID | None:
        return self._current_id

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
                await self._wait(self._empty_poll)
                continue

            for item in items:
                if self._stop_event.is_set():
                    break
                self._current_id = item.id
                self._safe_render(item)
                await self._wait(item.duration_ms / 1000)

    async def _wait(self, seconds: float) -> None:
        """Sleep up to `seconds`, returning early if stop is requested."""
        assert self._stop_event is not None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)

    def _safe_render(self, item: ContentItem) -> None:
        try:
            self._render_item(item)
        except Exception:
            log.exception("playback: failed to render %s", item.id)

    def _render_item(self, item: ContentItem) -> None:
        try:
            asset_bytes = self._read_asset(item.id)
        except FileNotFoundError:
            log.warning("playback: asset missing for %s, skipping", item.id)
            return

        try:
            image = Image.open(io.BytesIO(asset_bytes)).convert("RGB")
        except UnidentifiedImageError:
            log.warning("playback: corrupt asset for %s, skipping", item.id)
            return

        if image.size != (self._renderer.width, self._renderer.height):
            image = image.resize(
                (self._renderer.width, self._renderer.height),
                resample=Image.Resampling.NEAREST,
            )
        self._renderer.render_frame(image.tobytes())
