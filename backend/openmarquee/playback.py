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

Phase 5b deferral: TextSlides can reference a VideoSlide via
`background_video_slide_id` (SYSTEM_SPEC §5.10). The browser-side
inline preview already composites the slide's text over the live video
frames at this commit. The device-side composite path — alpha-blend
text PNG over .rgb panel frames + ffmpeg `overlay` filter for HDMI —
is the same shape but needs a real video frame stream first, which
arrives in Phase 6. Tracking as Phase 5c. Until then this loop also
treats Text-over-Video slides as still thumbnails (the editor saves
a flattened text-over-thumbnail PNG as a fallback — see
content/__init__.py::TextSlide::background_video_slide_id docstring).

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

import numpy as np
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
        # Pause/resume for stream takeover (SYSTEM_SPEC §5.11). When a
        # stream session is active, it pause()s the loop, takes over
        # render_frame() calls itself, then resume()s when done. Two
        # events because asyncio.Event has no "wait for clear": _pause
        # set means "stop rendering and yield", _resume set means
        # "start rendering again". They're mutually exclusive — pause()
        # and resume() flip them as a pair.
        self._pause_event: asyncio.Event | None = None
        self._resume_event: asyncio.Event | None = None
        # Index in the current items[] where the loop was when pause
        # took effect. None when not paused; set so the loop resumes
        # at the same slide rather than restarting the playlist from 0.
        # Sub-second position-within-slide is NOT tracked — the slide
        # plays for its full duration on resume.
        self._resume_at_index: int | None = None
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
        self._current_playlist_id: UUID | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def is_paused(self) -> bool:
        """True when a pause has been requested and not yet resumed.

        Stream takeover (SYSTEM_SPEC §5.11) flips this on while a
        WebRTC session is active so the playback loop yields the
        renderer to the stream's frame source.
        """
        return self._pause_event is not None and self._pause_event.is_set()

    @property
    def renderer(self) -> Renderer:
        """The renderer the loop drives. Exposed so a paused loop's
        external frame source (e.g. the stream session) can push frames
        through the same wire format the loop normally uses, instead of
        instantiating a parallel renderer."""
        return self._renderer

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
        # Bind the Events to the running event loop on each start.
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._resume_event = asyncio.Event()
        # Initial state: not paused, so resume_event is set so any caller
        # that checks "are we allowed to run?" sees true. _pause_event is
        # the inverse signal — set means "yield."
        self._resume_event.set()
        self._resume_at_index = None
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
            self._pause_event = None
            self._resume_event = None
            self._resume_at_index = None
            self._current_id = None
            self._current_type = None
            self._current_transition = None
            self._current_transition_ms = None
            self._current_auto_mode = None
            self._current_auto_format = None
            self._current_playlist_id = None

    async def _loop(self) -> None:
        assert self._stop_event is not None
        assert self._pause_event is not None
        while not self._stop_event.is_set():
            # If pause was requested while we were between iterations,
            # wait here for resume (or stop) before fetching items.
            if self._pause_event.is_set():
                await self._wait_for_resume()
                if self._stop_event.is_set():
                    break

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

            # Resume mid-cycle if a previous iteration was interrupted by
            # pause. If the items list shrank during pause and the saved
            # index now points past the end, fall back to 0. Explicit
            # None-check rather than `or 0` so a legitimate save of
            # index 0 doesn't degrade through the falsy-coalesce.
            start_idx = 0 if self._resume_at_index is None else self._resume_at_index
            self._resume_at_index = None
            if start_idx >= len(items):
                start_idx = 0

            # Pre-load images lazily inside the per-item iteration so a
            # mid-cycle add/delete is reflected on the next pass without
            # ballooning memory for a hundred-slide playlist.
            for i in range(start_idx, len(items)):
                if self._stop_event.is_set():
                    break

                # Pause-check at the top of each iteration: if a stream
                # takeover requested pause, save where we are so the
                # outer-while resumes here, and yield the renderer.
                if self._pause_event.is_set():
                    self._resume_at_index = i
                    break

                item = items[i]
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

                # If the wait was woken by a pause request mid-slide,
                # skip the transition and resume at the same index so
                # the user sees the same slide when stream stops.
                if self._pause_event.is_set():
                    self._resume_at_index = i
                    break

                # Transition into the next slide (wraps to first). "cut"
                # is instant → no-op. single-item playlists also no-op
                # (next == current). Honors the current item's setting
                # all the way through the last-to-first wrap so the
                # inline preview and the device stay consistent.
                if (
                    item.transition
                    in (
                        "fade",
                        "wipe",
                        "slide",
                        "iris",
                        "scroll",
                        "flip",
                        "marquee",
                        "dissolve",
                        "pixelate",
                        "halftone",
                        "scanline",
                        "glitch",
                    )
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
                        elif kind == "scroll":
                            await self._scroll(current_image, next_image, item.transition_ms)
                        elif kind == "flip":
                            await self._flip(current_image, next_image, item.transition_ms)
                        elif kind == "marquee":
                            await self._marquee(current_image, next_image, item.transition_ms)
                        elif kind == "dissolve":
                            await self._dissolve(current_image, next_image, item.transition_ms)
                        elif kind == "pixelate":
                            await self._pixelate(current_image, next_image, item.transition_ms)
                        elif kind == "halftone":
                            await self._halftone(current_image, next_image, item.transition_ms)
                        elif kind == "scanline":
                            await self._scanline(current_image, next_image, item.transition_ms)
                        elif kind == "glitch":
                            await self._glitch(current_image, next_image, item.transition_ms)

    async def _wait(self, seconds: float) -> None:
        """Sleep up to `seconds`, returning early on stop or pause request.

        Pause-awareness keeps stream takeover responsive: without it, a
        5-second slide on screen would mean up to 5s of playlist render
        before the stream session's pause() actually yields the renderer.
        """
        assert self._stop_event is not None
        assert self._pause_event is not None
        stop_task = asyncio.create_task(self._stop_event.wait())
        pause_task = asyncio.create_task(self._pause_event.wait())
        try:
            await asyncio.wait(
                [stop_task, pause_task],
                timeout=seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (stop_task, pause_task):
                t.cancel()
                # Suppress the swallow-the-cancel exception if a task
                # already completed before we tried to cancel it.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t

    async def _wait_for_resume(self) -> None:
        """Block until resume_event is set OR stop_event is set.

        Used by the outer-while when a stream takeover has paused the
        loop — we yield indefinitely (no rendering, no advancing) until
        the stream session ends or the loop is asked to stop entirely.
        """
        assert self._resume_event is not None
        assert self._stop_event is not None
        resume_task = asyncio.create_task(self._resume_event.wait())
        stop_task = asyncio.create_task(self._stop_event.wait())
        try:
            await asyncio.wait(
                [resume_task, stop_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (resume_task, stop_task):
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t

    async def pause(self) -> None:
        """Request the loop yield rendering. No-op if not running.

        Used by Stream takeover (SYSTEM_SPEC §5.11): when a WebRTC
        session activates, it pause()s the loop, takes over render_frame
        calls itself, then resume()s when the session ends. Idempotent —
        repeated pause() calls don't accumulate state.
        """
        if self._pause_event is None or self._resume_event is None:
            return
        self._resume_event.clear()
        self._pause_event.set()

    async def resume(self) -> None:
        """Resume after pause. No-op if not paused or not running.

        The loop wakes from _wait_for_resume, refetches items, and
        continues at the saved index — same slide that was on screen
        before pause (sub-second position-within-slide is not tracked,
        so that slide plays for its full duration on resume).
        """
        if self._pause_event is None or self._resume_event is None:
            return
        self._pause_event.clear()
        self._resume_event.set()

    async def _play_auto_slide(self, item: ContentItem) -> Image.Image | None:
        """Tick-render an auto-mode text slide for its full duration.

        Re-composites the frame every `self._auto_tick` seconds using
        the current time in the configured timezone, so a 'time' slide
        with HH:MM:SS format visibly ticks seconds on the device.

        Returns the last-composed frame so the caller's fade transition
        into the next slide has something to fade from. Returns None if
        stop or pause fires before the first frame lands. Pause exits
        early so a stream takeover doesn't keep painting auto frames
        over the live video — same rationale as the transition helpers.
        """
        tz = resolve_timezone(self._get_timezone())
        total = item.duration_ms / 1000
        end_at = asyncio.get_event_loop().time() + total
        last: Image.Image | None = None
        while True:
            assert self._stop_event is not None
            assert self._pause_event is not None
            if self._stop_event.is_set() or self._pause_event.is_set():
                return last
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

            if self._stop_event.is_set() or self._pause_event.is_set():
                return last

            remaining = end_at - asyncio.get_event_loop().time()
            if remaining <= 0:
                return last
            await self._wait(min(self._auto_tick, remaining))
            if self._stop_event.is_set() or self._pause_event.is_set():
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
    def current_playlist_id(self) -> UUID | None:
        """The playlist id the loop is currently sourcing items from.

        Set by the schedule-driven fetch fn at the start of each iteration so
        the UI can show "now playing from <playlist>". None when not running.
        """
        return self._current_playlist_id

    def _stamp_playlist_id(self, playlist_id: UUID | None) -> None:
        """Hook for the scheduled fetch fn to publish which playlist is
        currently active. Test-only setter is just self._current_playlist_id."""
        self._current_playlist_id = playlist_id

    async def _fade(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Alpha-blend from `from_image` to `to_image` over `transition_ms`.

        Returns early on stop OR pause request — pause-awareness keeps
        the transition from painting playlist frames over an in-flight
        stream takeover. Image.blend is the actual per-pixel math — at
        alpha=0 we get from_image; at alpha=1 we get to_image.
        """
        n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
        frame_period = (transition_ms / 1000) / n_frames
        for i in range(1, n_frames + 1):
            assert self._stop_event is not None
            assert self._pause_event is not None
            if self._stop_event.is_set() or self._pause_event.is_set():
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
            assert self._pause_event is not None
            if self._stop_event.is_set() or self._pause_event.is_set():
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
            assert self._pause_event is not None
            if self._stop_event.is_set() or self._pause_event.is_set():
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

    async def _scroll(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Vertical scroll: `from_image` rolls up off the top while
        `to_image` rolls in from the bottom at the same rate. Reads as
        a stadium scoreboard advancing rows. Distinct from slide in
        that the motion is vertical — natural on tall WS281x columns
        and ticker-style HUB75 strips."""
        n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
        frame_period = (transition_ms / 1000) / n_frames
        width, height = from_image.size
        for i in range(1, n_frames + 1):
            assert self._stop_event is not None
            assert self._pause_event is not None
            if self._stop_event.is_set() or self._pause_event.is_set():
                return
            offset = max(0, min(height, int(round(height * i / n_frames))))
            frame = Image.new("RGB", (width, height))
            # from_image: shifted up by `offset`, so rows [offset, height)
            # of the source go to rows [0, height - offset) of the frame.
            if offset < height:
                frame.paste(from_image.crop((0, offset, width, height)), (0, 0))
            # to_image: enters from the bottom edge — its topmost
            # `offset` rows go to rows [height - offset, height).
            if offset > 0:
                frame.paste(to_image.crop((0, 0, width, offset)), (0, height - offset))
            self._render_image(frame)
            await self._wait(frame_period)

    async def _flip(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Card-flip: from_image scaleX-shrinks to a center column over the
        first half, then to_image scaleX-grows from a center column over
        the second half. 2D approximation of a 3D card-flip — at small
        panel sizes the suggestion of "flipping" carries even without
        perspective.

        Strip-graceful: a horizontal scaleX flip on a width<2 panel has
        no visible motion (the only column collapses to itself), so we
        fall back to fade. Per QA's 2026-04-28 transition-palette spec:
        "flip on a strip is meaningless — fall back to fade."
        """
        width, height = from_image.size
        if width < 2:
            await self._fade(from_image, to_image, transition_ms)
            return

        n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
        frame_period = (transition_ms / 1000) / n_frames
        for i in range(1, n_frames + 1):
            assert self._stop_event is not None
            assert self._pause_event is not None
            if self._stop_event.is_set() or self._pause_event.is_set():
                return
            progress = i / n_frames
            # First half: from_image shrinks 1.0 → 0.0 width.
            # Second half: to_image grows 0.0 → 1.0 width.
            if progress < 0.5:
                scale = 1.0 - 2.0 * progress
                source = from_image
            else:
                scale = 2.0 * progress - 1.0
                source = to_image
            new_w = max(1, int(round(width * scale)))
            # Horizontal squish only — height is preserved so the card
            # reads as flipping around a vertical axis, not collapsing.
            resized = source.resize((new_w, height))
            frame = Image.new("RGB", (width, height))
            frame.paste(resized, ((width - new_w) // 2, 0))
            self._render_image(frame)
            await self._wait(frame_period)

    async def _marquee(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Tickertape wraparound: from_image scrolls off to the left, a
        gap with a centered dot separator passes through, and to_image
        arrives from the right. Native to the openMarquee brand identity
        — the same "ticker" reading that the wordmark evokes.

        Implemented by composing a wide [from | gap | to] strip and
        sliding a width-sized window across it. Cleaner than tracking
        three independent paste offsets per frame.

        Strip-graceful: width<2 → no horizontal motion is meaningful,
        fall back to fade. Per QA's spec for strip rendering.
        """
        from PIL import ImageDraw

        width, height = from_image.size
        if width < 2:
            await self._fade(from_image, to_image, transition_ms)
            return

        # Gap is ~1/8 of canvas width, min 4px so the dot stays visible
        # on small panels. The compound is [from | gap | to] = total
        # 2*width + gap_w wide; the visible window is `width` wide so
        # the scroll distance over transition_ms is width + gap_w (after
        # which to_image is fully revealed).
        gap_w = max(4, width // 8)
        gap_panel = Image.new("RGB", (gap_w, height))
        # Centered dot — small filled circle. dot_radius bounded by both
        # gap width and panel height so it never bleeds the gap or
        # crowds a thin row. Falls to 1px on a 1-row strip.
        dot_radius = max(1, min(gap_w // 3, height // 3))
        cx, cy = gap_w // 2, height // 2
        ImageDraw.Draw(gap_panel).ellipse(
            (cx - dot_radius, cy - dot_radius, cx + dot_radius, cy + dot_radius),
            fill=(255, 255, 255),
        )
        compound = Image.new("RGB", (2 * width + gap_w, height))
        compound.paste(from_image, (0, 0))
        compound.paste(gap_panel, (width, 0))
        compound.paste(to_image, (width + gap_w, 0))

        scroll_total = width + gap_w
        n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
        frame_period = (transition_ms / 1000) / n_frames
        for i in range(1, n_frames + 1):
            assert self._stop_event is not None
            assert self._pause_event is not None
            if self._stop_event.is_set() or self._pause_event.is_set():
                return
            offset = max(0, min(scroll_total, int(round(scroll_total * i / n_frames))))
            frame = compound.crop((offset, 0, offset + width, height))
            self._render_image(frame)
            await self._wait(frame_period)

    async def _dissolve(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Random per-pixel reveal: each pixel of `to_image` is gated by
        a per-pixel threshold sampled uniformly from [0, 1). As progress
        crosses each pixel's threshold, that pixel switches from
        `from_image` to `to_image`. Reads as a noise-driven crossfade
        — the first of the dot-matrix-family transitions per the
        2026-04-28 palette spec.

        Strip-graceful: works at any width or height — the per-pixel
        randomization doesn't depend on geometry, so a 1×N strip just
        sees its individual pixels reveal independently. No fallback.
        """
        width, height = from_image.size
        # Per-pixel reveal thresholds. Generated once per transition so
        # pixels reveal in a stable random order across frames; using a
        # fresh rng each call means no two transitions share a pattern.
        thresholds = np.random.default_rng().random((height, width))

        n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
        frame_period = (transition_ms / 1000) / n_frames
        for i in range(1, n_frames + 1):
            assert self._stop_event is not None
            assert self._pause_event is not None
            if self._stop_event.is_set() or self._pause_event.is_set():
                return
            progress = i / n_frames
            # Binary L-mode mask: 255 wherever a pixel has been
            # revealed (threshold < progress), 0 elsewhere.
            # Image.composite picks to_image where mask==255, from_image
            # where mask==0 — exactly the per-pixel switch we want.
            mask_arr = (thresholds < progress).astype(np.uint8) * 255
            mask = Image.fromarray(mask_arr, mode="L")
            frame = Image.composite(to_image, from_image, mask)
            self._render_image(frame)
            await self._wait(frame_period)

    async def _pixelate(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Chunky-pixel cross-fade: both images pixelate to a peak block
        size at the midpoint then sharpen back to native resolution as
        the alpha-blend progresses from-image to to-image. Reads as the
        slide is "rendering" into the next at progressively coarser
        then finer dot-matrix resolution — second of the dot-matrix-
        family transitions per the 2026-04-28 palette spec.

        Strip-graceful: with width<2 or height<2 there's no room to
        pixelate (block_size collapses to 1 → identity), so delegate
        to fade. Cleaner than emitting frames identical to fade under
        a different name.
        """
        width, height = from_image.size
        if width < 2 or height < 2:
            await self._fade(from_image, to_image, transition_ms)
            return

        # Peak block size: ~quarter of the smaller dimension. Bounded
        # so even tall-skinny strips (e.g. 4×64) don't try to pixelate
        # into a single super-pixel that erases all content.
        max_block = max(2, min(width, height) // 4)

        n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
        frame_period = (transition_ms / 1000) / n_frames
        for i in range(1, n_frames + 1):
            assert self._stop_event is not None
            assert self._pause_event is not None
            if self._stop_event.is_set() or self._pause_event.is_set():
                return
            progress = i / n_frames
            # Triangular: 0 → 1 → 0 over [0, 0.5, 1]. Block size grows
            # then shrinks. Linear cross-fade over the same window.
            triangular = 1.0 - abs(2.0 * progress - 1.0)
            block_size = max(1, int(round(1 + triangular * (max_block - 1))))
            small_w = max(1, width // block_size)
            small_h = max(1, height // block_size)
            # NEAREST resample twice = pixelation. Down-sample first to
            # reduce detail, then up-sample to reproduce as blocky
            # squares.
            pix_from = from_image.resize(
                (small_w, small_h), Image.NEAREST
            ).resize((width, height), Image.NEAREST)
            pix_to = to_image.resize(
                (small_w, small_h), Image.NEAREST
            ).resize((width, height), Image.NEAREST)
            frame = Image.blend(pix_from, pix_to, progress)
            self._render_image(frame)
            await self._wait(frame_period)

    async def _halftone(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Halftone-dot reveal: to_image emerges through a regular grid
        of growing circular dots, one per cell. Reads as the next slide
        is "printing in" through a dot-matrix screen — closes the
        dot-matrix family per the 2026-04-28 palette spec.

        Cell pitch = max(2, min(width, height) // 8) — gives roughly an
        8-cell row across the smaller dimension. Per-cell dot radius
        grows linearly 0 → max_r over transition_ms. max_r is the
        cell's half-diagonal (≈ pitch * 0.71), so by progress=1 every
        circle covers its cell entirely → fully revealed to_image.

        Strip-graceful: width<4 or height<4 leaves nothing for the dot
        grid to cohere into (a single column of cells just degenerates
        to a stripe), so delegate to fade.
        """
        from PIL import ImageDraw

        width, height = from_image.size
        if width < 4 or height < 4:
            await self._fade(from_image, to_image, transition_ms)
            return

        pitch = max(2, min(width, height) // 8)
        # Half-diagonal of a square cell: pitch * sqrt(2)/2 ≈ 0.707 *
        # pitch. +1 ensures rounding never leaves a hairline gap at
        # progress=1.
        max_r = int(round(pitch * 0.71)) + 1

        # Cell centers — staggered grid offset by half-pitch so cells
        # are inset from canvas edges. Computed once per transition.
        cell_centers: list[tuple[int, int]] = []
        cy = pitch // 2
        while cy < height:
            cx = pitch // 2
            while cx < width:
                cell_centers.append((cx, cy))
                cx += pitch
            cy += pitch

        n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
        frame_period = (transition_ms / 1000) / n_frames
        for i in range(1, n_frames + 1):
            assert self._stop_event is not None
            assert self._pause_event is not None
            if self._stop_event.is_set() or self._pause_event.is_set():
                return
            progress = i / n_frames
            radius = int(round(progress * max_r))
            mask = Image.new("L", (width, height), 0)
            draw = ImageDraw.Draw(mask)
            for cx, cy in cell_centers:
                draw.ellipse(
                    (cx - radius, cy - radius, cx + radius, cy + radius),
                    fill=255,
                )
            frame = Image.composite(to_image, from_image, mask)
            self._render_image(frame)
            await self._wait(frame_period)

    async def _scanline(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """CRT scanline sweep: a bright horizontal band sweeps top-to-
        bottom over transition_ms. Above the line is to_image; below
        stays from_image. Reads as a vintage tube reveal where the
        electron beam is "scanning in" the new frame. First of the CRT-
        family transitions per the 2026-04-28 palette spec.

        Strip-graceful: scanline on a 1-row strip (height<2) has no
        room to sweep — the line would cover the entire panel for the
        duration. Fall back to fade. Per QA's spec ("scanline on a 1×N
        strip is just a fade"); we make it explicit here so the
        operator's pulldown choice doesn't silently degrade.
        """
        from PIL import ImageDraw

        width, height = from_image.size
        if height < 2:
            await self._fade(from_image, to_image, transition_ms)
            return

        # Sweep band thickness: ~3% of panel height, min 1px. Gives a
        # visible CRT-glow trail without dominating short canvases.
        band_height = max(1, height // 32)

        n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
        frame_period = (transition_ms / 1000) / n_frames
        for i in range(1, n_frames + 1):
            assert self._stop_event is not None
            assert self._pause_event is not None
            if self._stop_event.is_set() or self._pause_event.is_set():
                return
            progress = i / n_frames
            sweep_y = int(round(progress * height))
            frame = from_image.copy()
            # Above the sweep: paint to_image rows.
            if sweep_y > 0:
                frame.paste(to_image.crop((0, 0, width, sweep_y)), (0, 0))
            # Bright glow band centered on the sweep line — clamped so
            # it doesn't extend past the canvas at progress=0 or 1.
            band_top = max(0, sweep_y - band_height // 2)
            band_bot = min(height, band_top + band_height)
            if band_bot > band_top:
                ImageDraw.Draw(frame).rectangle(
                    (0, band_top, width - 1, band_bot - 1),
                    fill=(255, 255, 255),
                )
            self._render_image(frame)
            await self._wait(frame_period)

    async def _glitch(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Digital-corruption-style transition: per-row horizontal jitter
        + linear cross-fade + occasional cyan "tear" rows that simulate
        broken video signal. Closes the CRT family per the 2026-04-28
        palette spec.

        Per-frame randomization (jitter + tear-row positions are
        regenerated each frame, not stable across frames like dissolve)
        is what gives the transition its "glitchy" animated quality —
        the screen-tearing effect can't read as broken if the breakage
        sits still.

        Strip-graceful: works at any geometry. Per-row jitter is
        shape-agnostic; even a 1×N strip just sees its single row jitter
        each frame. No fallback needed (and none added). Per QA's spec.
        """
        width, height = from_image.size
        rng = np.random.default_rng()
        # Jitter ceiling ~10% of canvas width, min 1. Bigger jitter
        # makes the glitch read more "broken"; small enough that the
        # underlying image stays mostly recognizable.
        max_jitter = max(1, width // 10)
        # Tear-row count ~5% of canvas height, min 1. Empirically the
        # smallest count that reads as "consistent corruption" rather
        # than "occasional artifact"; tunable later if QA wants more.
        n_tears = max(1, height // 20)

        from_arr = np.asarray(from_image, dtype=np.uint8)
        to_arr = np.asarray(to_image, dtype=np.uint8)

        n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
        frame_period = (transition_ms / 1000) / n_frames
        for i in range(1, n_frames + 1):
            assert self._stop_event is not None
            assert self._pause_event is not None
            if self._stop_event.is_set() or self._pause_event.is_set():
                return
            progress = i / n_frames
            # Per-row x-shift, regenerated this frame.
            shifts = rng.integers(-max_jitter, max_jitter + 1, size=height)
            shifted_from = np.empty_like(from_arr)
            for y in range(height):
                shifted_from[y] = np.roll(from_arr[y], shifts[y], axis=0)
            # Linear cross-fade.
            blended = (
                shifted_from.astype(np.float32) * (1.0 - progress)
                + to_arr.astype(np.float32) * progress
            ).astype(np.uint8)
            # Inject cyan tear rows. The exact (0, 255, 255) triplet is
            # the test-time discriminator — R=0 + G=255 + B=255 all
            # simultaneously is impossible from a red↔blue cross-fade
            # (which stays in the R-B plane: G=0 always). Marquee and
            # scanline DO paint G=255 elsewhere — but as white (255,
            # 255, 255), not cyan — so the full triplet stays unique
            # to glitch's tear-row injection.
            tear_rows = rng.choice(
                height, size=min(n_tears, height), replace=False
            )
            for ty in tear_rows:
                blended[ty] = (0, 255, 255)
            frame = Image.fromarray(blended, mode="RGB")
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
            assert self._pause_event is not None
            if self._stop_event.is_set() or self._pause_event.is_set():
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

    If `loop` is provided, stamps `_current_playlist_id` so the UI can show
    which playlist is active. The PlaybackLoop's fetch_items closure passes
    itself in via `partial`.
    """
    from openmarquee.playlist import list_in_playlist_order
    from openmarquee.schedule import evaluate_schedule

    schedule = schedule_storage.load()
    active_id = evaluate_schedule(now, schedule)
    if loop is not None:
        loop._stamp_playlist_id(active_id)
    return list_in_playlist_order(content_storage, playlist_storage, active_id)


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
