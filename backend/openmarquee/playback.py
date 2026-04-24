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

from openmarquee.auto_render import compose_auto_frame, resolve_timezone
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
        get_timezone: Callable[[], str | None] | None = None,
        auto_tick_seconds: float = 1.0,
    ):
        self._renderer = renderer
        self._fetch_items = fetch_items
        self._read_asset = read_asset
        self._empty_poll = empty_playlist_poll_seconds
        # Returns an IANA timezone name (e.g. "America/Los_Angeles") so
        # auto-mode text slides render in the operator-configured zone.
        # Returning None falls back to UTC. Tests inject a fixed value
        # so assertions don't depend on the environment's tz.
        self._get_timezone = get_timezone or (lambda: None)
        # How often an auto-mode slide re-renders. 1Hz is the right
        # cadence for a ticking-seconds display; tests override to a
        # much smaller value to keep runtime fast.
        self._auto_tick = auto_tick_seconds
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._current_id: UUID | None = None
        # Exposed alongside _current_id so the UI's live preview knows
        # whether to render a <video> or a <img> without a second round
        # trip to /api/content/{id}.
        self._current_type: str | None = None
        # The outgoing transition + transition_ms (set by the PlaylistItem
        # the loop is currently rendering). The live preview stashes the
        # most recent non-null values so that when current_item_id changes,
        # it can run a matching CSS cross-fade against the OLD item's
        # transition metadata.
        self._current_transition: str | None = None
        self._current_transition_ms: int | None = None
        # Auto-mode metadata for the currently-rendering TextSlide (None
        # on image/video/non-auto text). The live preview uses these to
        # overlay a ticking time/date/day client-side so the browser
        # preview stays in sync with what the device is actually
        # rendering, without asking the server for a fresh PNG each tick.
        self._current_auto_mode: str | None = None
        self._current_auto_format: str | None = None
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

    @property
    def current_item_transition(self) -> str | None:
        return self._current_transition

    @property
    def current_item_transition_ms(self) -> int | None:
        return self._current_transition_ms

    @property
    def current_item_auto_mode(self) -> str | None:
        return self._current_auto_mode

    @property
    def current_item_auto_format(self) -> str | None:
        return self._current_auto_format

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
            self._current_transition = None
            self._current_transition_ms = None
            self._current_auto_mode = None
            self._current_auto_format = None
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
                self._current_transition = None
                self._current_transition_ms = None
                self._current_auto_mode = None
                self._current_auto_format = None
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
                self._current_transition = item.transition
                self._current_transition_ms = item.transition_ms
                self._current_auto_mode = (
                    getattr(item, "auto_mode", None)
                    if item.type == "text_slide"
                    else None
                )
                self._current_auto_format = (
                    getattr(item, "auto_format", None)
                    if item.type == "text_slide"
                    else None
                )

                is_auto = (
                    item.type == "text_slide"
                    and getattr(item, "auto_mode", None) is not None
                )
                if is_auto:
                    # Render-over path: the stored PNG is a placeholder;
                    # compose a fresh frame with current time/date/day
                    # each tick for the slide's full duration.
                    current_image = await self._play_auto_slide(item)
                    if current_image is None:
                        continue
                else:
                    current_image = self._safe_load_image(item)
                    if current_image is None:
                        continue
                    self._render_image(current_image)
                    await self._wait(item.duration_ms / 1000)

                if self._stop_event.is_set():
                    break

                # Transition into the next slide (wraps to first). "cut"
                # is instant → no-op. single-item playlists also no-op
                # (next == current). Honors the current item's setting
                # all the way through the last-to-first wrap so the
                # inline preview and the device stay consistent.
                if (
                    item.transition in ("fade", "wipe", "slide", "iris")
                    and item.transition_ms > 0
                    and len(items) > 1
                ):
                    next_item = items[(i + 1) % len(items)]
                    next_image = self._safe_load_image(next_item)
                    if next_image is not None:
                        kind = item.transition
                        if kind == "fade":
                            await self._fade(current_image, next_image, item.transition_ms)
                        elif kind == "wipe":
                            await self._wipe(current_image, next_image, item.transition_ms)
                        elif kind == "slide":
                            await self._slide(current_image, next_image, item.transition_ms)
                        elif kind == "iris":
                            await self._iris(current_image, next_image, item.transition_ms)

    async def _wait(self, seconds: float) -> None:
        """Sleep up to `seconds`, returning early if stop is requested."""
        assert self._stop_event is not None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)

    async def _play_auto_slide(self, item: ContentItem) -> Image.Image | None:
        """Tick-render an auto-mode text slide for its full duration.

        Re-composites the frame every `self._auto_tick` seconds using
        the current time in the configured timezone, so a 'time' slide
        with HH:MM:SS format visibly ticks seconds on the device.

        Returns the last-composed frame so the caller's fade transition
        into the next slide has something to fade from. Returns None if
        stop_event fires before the first frame lands.
        """
        tz = resolve_timezone(self._get_timezone())
        total = item.duration_ms / 1000
        end_at = asyncio.get_event_loop().time() + total
        last: Image.Image | None = None
        while True:
            now = datetime.now(tz)
            try:
                frame = compose_auto_frame(
                    item,
                    self._renderer.width,
                    self._renderer.height,
                    now,
                    read_asset=self._read_asset,
                )
            except Exception:
                log.exception("playback: compose_auto_frame failed for %s", item.id)
                return None
            self._render_image(frame)
            last = frame

            assert self._stop_event is not None
            if self._stop_event.is_set():
                return last

            remaining = end_at - asyncio.get_event_loop().time()
            if remaining <= 0:
                return last
            await self._wait(min(self._auto_tick, remaining))
            if self._stop_event.is_set():
                return last
            if asyncio.get_event_loop().time() >= end_at:
                return last

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

        target_w = self._renderer.width
        target_h = self._renderer.height
        if image.size != (target_w, target_h):
            image = _cover_fit(image, target_w, target_h)
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

    async def _slide(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Push transition: `from_image` slides off to the left while
        `to_image` slides in from the right at the same rate. Distinct
        from wipe in that BOTH frames move (rather than to_image
        revealing under a stationary from_image)."""
        n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
        frame_period = (transition_ms / 1000) / n_frames
        width, height = from_image.size
        for i in range(1, n_frames + 1):
            assert self._stop_event is not None
            if self._stop_event.is_set():
                return
            offset = max(0, min(width, int(round(width * i / n_frames))))
            frame = Image.new("RGB", (width, height))
            # from_image: shifted left by `offset`, so columns [offset, width)
            # of the source go to columns [0, width - offset) of the frame.
            if offset < width:
                frame.paste(from_image.crop((offset, 0, width, height)), (0, 0))
            # to_image: enters from the right edge — its leftmost
            # `offset` columns go to columns [width - offset, width).
            if offset > 0:
                frame.paste(to_image.crop((0, 0, offset, height)), (width - offset, 0))
            self._render_image(frame)
            await self._wait(frame_period)

    async def _iris(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Iris transition: `to_image` reveals through a circular mask
        that grows from a center pinpoint to fully cover the canvas.
        Reads as a film-projector aperture opening — distinct enough
        from fade and wipe at small panel sizes that it stays legible."""
        from PIL import ImageDraw

        n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
        frame_period = (transition_ms / 1000) / n_frames
        width, height = from_image.size
        # Final radius covers the whole canvas — corner-to-center distance.
        max_r = int(((width / 2) ** 2 + (height / 2) ** 2) ** 0.5) + 1
        cx, cy = width // 2, height // 2
        for i in range(1, n_frames + 1):
            assert self._stop_event is not None
            if self._stop_event.is_set():
                return
            radius = int(round(max_r * i / n_frames))
            mask = Image.new("L", (width, height), 0)
            ImageDraw.Draw(mask).ellipse(
                (cx - radius, cy - radius, cx + radius, cy + radius),
                fill=255,
            )
            frame = Image.composite(to_image, from_image, mask)
            self._render_image(frame)
            await self._wait(frame_period)

    async def _wipe(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Left-to-right wipe: `to_image` reveals from the left edge,
        pushing `from_image` out of the way over `transition_ms`.

        Returns early on stop. Same frame cadence as _fade so the two
        transitions feel like the same smoothness at equal transition_ms.
        """
        n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
        frame_period = (transition_ms / 1000) / n_frames
        width, height = from_image.size
        for i in range(1, n_frames + 1):
            assert self._stop_event is not None
            if self._stop_event.is_set():
                return
            split = max(0, min(width, int(round(width * i / n_frames))))
            # Compose: left `split` columns from to_image, rest from from_image.
            frame = from_image.copy()
            if split > 0:
                frame.paste(to_image.crop((0, 0, split, height)), (0, 0))
            self._render_image(frame)
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


def _cover_fit(image: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Scale `image` to cover (`target_w`, `target_h`) and center-crop.

    Preserves the source aspect — the larger dimension is resized up or
    down to exactly match the target, and the overflow on the other axis
    is cropped evenly on both sides. Mirrors the browser-side editor
    previews so what the operator sees IS what the device renders.
    """
    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w = max(1, round(src_w * scale))
    new_h = max(1, round(src_h * scale))
    # LANCZOS is the slower-but-sharper resample; for a one-shot render
    # at slide entry the ~10-15ms cost on a Pi Zero 2 W is invisible
    # behind the transition.
    resized = image.resize((new_w, new_h), resample=Image.Resampling.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))
