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

Replaced the Phase 2 manual `/dev/play/{id}` poke (the dev endpoint
has since been removed). The dev preview now updates continuously
while playback is running, like a real sign.
"""

import asyncio
import contextlib
import io
import logging
import os
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

import numpy as np
from PIL import Image, UnidentifiedImageError

from openmarquee.auto_render import resolve_timezone
from openmarquee.content import ContentItem
from openmarquee.motion import (
    compose_motion_frame,
    load_motion_background,
    prerender_layer_bitmaps,
    slide_has_dynamic_content,
    slide_has_motion,
)
from openmarquee.rendering import Renderer
from openmarquee.rendering.gpu_compositor import (
    GPUSlideCompositor,
    MultiPlaneRenderer,
    SlideAssetCache,
    classify_layer,
)

# Transition kinds with a fragment shader implementation in
# rendering.shader_compositor._TRANSITION_SHADERS. When the
# OPENMARQUEE_SHADER_TRANSITIONS env var is "1" AND the renderer
# exposes drm_fd (DRMRenderer in multi-plane mode), the dispatcher
# routes these kinds through ShaderRenderer instead of the PIL
# software path. fade and wipe are deliberately NOT here -- they
# already have their own fast plane-property animation paths
# (_fade_gpu / _wipe_gpu). New kinds get added here as their
# fragments land in _TRANSITION_SHADERS.
_SHADER_TRANSITION_KINDS = frozenset({
    "iris",
    "dissolve",
    "pixelate",
    "scanline",
    "halftone",
    "glitch",
    "slide",
    "push",
    "scroll",
    "blinds",
    "flip",
    "marquee",
    "shutter",
})


def _slide_has_animated_blend_mode_layer(item: ContentItem) -> bool:
    """True iff the slide has at least one layer with motion (or
    auto_mode) AND a non-normal blend mode. Such a layer can't run
    on the multi-plane DRM path -- vc4 HVS overlay planes only do
    alpha-blend (PREMULTI), not Photoshop blend modes -- so the GPU
    path is skipped and the slide drops to compose_motion_frame
    software path where blend modes are applied per-tick.
    Per-tick-blend at 1080p is heavy (one composite_with_blend call
    per tick = ~10 ms) but for the rare animated-blend-mode case it's
    the right trade-off until shader-path blend mode work lands."""
    if getattr(item, "type", None) != "text_slide":
        return False
    for layer in getattr(item, "text_layers", []):
        if classify_layer(layer) != "animated":
            continue
        blend = getattr(layer, "blend", None) or "normal"
        if blend != "normal":
            return True
    return False


def _count_animated_layers(item: ContentItem) -> int:
    """How many of `item`'s text layers will consume a DRM overlay
    plane on the GPU path. Mirrors GPUSlideCompositor's animated
    classification (motion non-static OR auto_mode set) — used by
    PlaybackLoop to decide whether the slide fits the renderer's
    plane budget. Returns 0 for non-text-slide content (image /
    video / etc.) so the GPU path is skipped naturally."""
    if getattr(item, "type", None) != "text_slide":
        return 0
    return sum(
        1 for layer in getattr(item, "text_layers", [])
        if classify_layer(layer) == "animated"
    )

if TYPE_CHECKING:
    from openmarquee.content import TextSlide
    from openmarquee.content.storage import ContentStorage
    from openmarquee.playlist import PlaylistStorage
    from openmarquee.rendering.shader_compositor import ShaderRenderer
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
        # Cross-slide PIL output cache for the GPU compositor's hot
        # path. First time through the playlist pays the bg+static
        # composite + glyph rasterization cost; subsequent reps reuse
        # the cached bytes and skip the 50-200 ms attach stall at
        # 1080p. Cleared on stop() so a new playlist gets fresh state.
        self._gpu_slide_cache = SlideAssetCache()
        # Snapshot cache for the shader-transition path (#205): a
        # transition's u_from + u_to RGBA snapshots are ~600 ms each
        # to compose at 1080p (PIL bg load + alpha_composite per
        # layer + blend math). Caching keyed by (slide.id, updated_at)
        # makes the second pass through a playlist instant. Slides
        # with auto-mode layers (clocks) skip the cache.
        from openmarquee.rendering.snapshot import SlideSnapshotCache
        self._snapshot_cache = SlideSnapshotCache()
        # Lazily-constructed shader compositor for slide-to-slide
        # transitions (iris/dissolve/etc.). Built on first use, reused
        # across every transition for the lifetime of the loop --
        # EGL/GL init is ~5 s on a cold mesa cache. None until either
        # _get_or_create_shader_renderer() succeeds or the feature is
        # disabled (env var unset, renderer lacks drm_fd, init failed).
        self._shader_renderer: "ShaderRenderer | None" = None
        # Lazily-flipped sentinel so we don't repeatedly attempt
        # ShaderRenderer construction after a failure (e.g. on dev hosts
        # without libdrm/libegl).
        self._shader_renderer_disabled: bool = False
        # Outgoing slide's GPUSlideCompositor, held alive across the
        # transition so animated text overlays keep moving (#206)
        # rather than freezing while the shader transitions the
        # bg+statics layer underneath. Set by _play_dynamic_slide_gpu
        # when shader transitions are enabled; consumed (and detached)
        # by _run_shader_transition or by _drain_outgoing_compositor
        # before non-shader transitions / new slide attaches.
        #
        # The outgoing compositor owns its overlay slots [0..N-1] until
        # drain. Future symmetric "fade-in for incoming compositor"
        # work must NOT pre-attach the next compositor before this one
        # drains -- they'd collide on slot indices. Either drain first
        # (sacrifice continuous incoming-side motion) or split the
        # plane budget statically (incoming gets [N/2..N-1]).
        self._outgoing_compositor: "GPUSlideCompositor | None" = None
        self._outgoing_slide: "TextSlide | None" = None
        # Slide-relative monotonic time at the moment the outgoing
        # slide started ticking. _tick_outgoing_during_transition
        # uses this as the elapsed_s base so motion phase stays
        # continuous across the handoff (fixes #207: ticker
        # specifically would otherwise snap back to far-right at
        # transition entry because phase=0 puts it at scroll start).
        self._outgoing_slide_t0: float | None = None
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
            # Drop GPU compositor PIL caches so a fresh start()
            # doesn't reuse stale (slide_id, updated_at) entries
            # against a content store that may have changed.
            self._gpu_slide_cache.clear()
            self._snapshot_cache.clear()
            # Tear down the shader compositor if it was lazily built.
            # ShaderRenderer holds DRM/EGL/GL state that survives
            # individual transitions but should be released alongside
            # the loop's lifecycle. close() is idempotent and safe in
            # shared-fd mode (won't blank the CRTC; caller's DRMRenderer
            # keeps owning master + scanout).
            if self._shader_renderer is not None:
                try:
                    self._shader_renderer.close()
                except Exception:
                    log.exception(
                        "playback: shader renderer close during stop failed"
                    )
                self._shader_renderer = None
            self._shader_renderer_disabled = False
            self._drain_outgoing_compositor()

    def _drain_outgoing_compositor(self) -> None:
        """Detach the outgoing slide's compositor (#206 cleanup) if one
        is being held alive across a transition. Idempotent. Called
        BEFORE non-shader transitions (so overlay slot 0 is free for
        _fade_gpu / _wipe_gpu), AFTER any transition that didn't claim
        the compositor itself, and on stop()."""
        c = self._outgoing_compositor
        self._outgoing_compositor = None
        self._outgoing_slide = None
        self._outgoing_slide_t0 = None
        if c is None:
            return
        try:
            c.detach()
        except Exception:
            log.exception("playback: outgoing compositor detach failed")

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
                # Schema v3 (qarl 2026-05-01): per-text fields live on
                # text_layers[0]. Phase 1 reads layer[0]; phase 2 of the
                # layered rollout will composite all layers and the
                # state-endpoint fields will reflect the active layer.
                if item.type == "text_slide" and item.text_layers:
                    primary_layer = item.text_layers[0]
                    self._current_auto_mode = primary_layer.auto_mode
                    self._current_auto_format = primary_layer.auto_format
                else:
                    self._current_auto_mode = None
                    self._current_auto_format = None

                is_dynamic = (
                    item.type == "text_slide"
                    and slide_has_dynamic_content(item)
                )
                if is_dynamic:
                    # Unified per-tick composer (docs/text-layer-motion-
                    # spec.md): any visible layer with motion != static OR
                    # auto_mode set drives a per-tick re-composition. The
                    # composer handles both in one pass — a clock that
                    # bounces gets its text refreshed AND its bitmap
                    # bounced each tick. Tick rate adapts: 30 Hz when
                    # motion is present, 1 Hz when only auto (avoids
                    # burning 30 fps for clock-only slides).
                    current_image = await self._play_dynamic_slide(item)
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
                        "push",
                        "blinds",
                        "shutter",
                    )
                    and item.transition_ms > 0
                    and len(items) > 1
                ):
                    next_item = items[(i + 1) % len(items)]
                    next_image = self._safe_load_image(next_item)
                    if next_image is not None:
                        kind = item.transition
                        # If the next transition isn't shader-routed
                        # (or shader transitions are off), drain the
                        # outgoing compositor NOW so its overlay slots
                        # are free for _fade_gpu / _wipe_gpu / etc.
                        # Shader-routed transitions get the compositor
                        # passed through via self._outgoing_compositor
                        # and detach it themselves.
                        if (
                            self._outgoing_compositor is not None
                            and kind not in _SHADER_TRANSITION_KINDS
                        ):
                            self._drain_outgoing_compositor()
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
                        elif kind == "push":
                            await self._push(current_image, next_image, item.transition_ms)
                        elif kind == "blinds":
                            await self._blinds(current_image, next_image, item.transition_ms)
                        elif kind == "shutter":
                            await self._shutter(current_image, next_image, item.transition_ms)

                # Catch-all: drain any compositor still alive after the
                # transition. Shader-routed transitions normally detach
                # inside _run_shader_transition, but if shader was
                # unavailable (env off, exception, fall-through to PIL)
                # the compositor stays here and would otherwise leak
                # into the next slide. Idempotent.
                self._drain_outgoing_compositor()

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

    async def _play_dynamic_slide(self, item: ContentItem) -> Image.Image | None:
        """Tick-render a slide with auto-mode and/or motion layers for
        its full duration.

        Two paths share this entrypoint:

        - **GPU path** (HDMI 1080p target, qarl 2026-05-02): when the
          renderer satisfies the MultiPlaneRenderer protocol AND has
          enough animated-plane budget for the slide, route through
          `GPUSlideCompositor` — bg + every static layer software-
          composite into the primary plane once at slide entry, each
          animated layer takes its own DRM overlay plane, per-tick
          motion is one atomic ioctl. Zero per-pixel CPU work in the
          inner loop.
        - **Software path** (LED matrices, dev mock, anything without
          multi-plane): per-tick `compose_motion_frame` builds an
          RGB frame and pushes it through `render_frame`. Same code
          path that has shipped since 5e75cf5.

        Selection is capability + budget gated. Slides that exceed the
        plane budget (rare; default vc4 budget is well above any real
        slide's animated count) fall back to software so playback
        keeps working. Tick cadence (30 Hz motion / 1 Hz auto-only) is
        identical across paths.

        Returns the last-composed frame so the caller's transition has
        something to fade from. Stop / pause exit early — same
        rationale as the prior split functions: a stream takeover
        shouldn't keep painting frames over live video.
        """
        if (
            item.type == "text_slide"
            and isinstance(self._renderer, MultiPlaneRenderer)
            and _count_animated_layers(item)
            <= getattr(self._renderer, "max_animated_planes", 0)
            and not _slide_has_animated_blend_mode_layer(item)
        ):
            try:
                return await self._play_dynamic_slide_gpu(item)
            except Exception:
                # Hard-fail in the GPU path falls back to software so a
                # broken plane attach (e.g. transient kernel error on
                # the dev Pi) doesn't take playback down with it.
                log.exception(
                    "playback: GPU compositor failed for %s, falling back",
                    item.id,
                )
        return await self._play_dynamic_slide_software(item)

    async def _play_dynamic_slide_software(
        self, item: ContentItem
    ) -> Image.Image | None:
        """Software path: per-tick compose_motion_frame → render_frame.
        The original implementation that has shipped since 5e75cf5."""
        tz = resolve_timezone(self._get_timezone())
        total = item.duration_ms / 1000
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        end_at = t0 + total
        # 30 Hz when motion is present, 1 Hz for auto-only — preserves
        # the prior _play_auto_slide cadence and avoids burning 29
        # frames of work per second for clock-only slides.
        if slide_has_motion(item):
            tick_period = 1.0 / max(1, _FADE_FPS)
        else:
            tick_period = max(0.1, self._auto_tick)
        last: Image.Image | None = None
        # Pre-load the background once. Pre-rasterize static layers
        # once. Auto layers leave None placeholders in the cache —
        # they re-render text each tick from `now`.
        try:
            background_cache: Image.Image | None = load_motion_background(
                item, self._renderer.width, self._renderer.height, self._read_asset,
            )
        except Exception:
            background_cache = None
        try:
            layer_bitmap_cache: list[Image.Image | None] | None = prerender_layer_bitmaps(
                item, self._renderer.width, self._renderer.height,
            )
        except Exception:
            layer_bitmap_cache = None
        while True:
            assert self._stop_event is not None
            assert self._pause_event is not None
            if self._stop_event.is_set() or self._pause_event.is_set():
                return last
            elapsed = loop.time() - t0
            now = datetime.now(tz)
            try:
                frame = compose_motion_frame(
                    item,
                    elapsed,
                    self._renderer.width,
                    self._renderer.height,
                    read_asset=self._read_asset,
                    background_cache=background_cache,
                    layer_bitmap_cache=layer_bitmap_cache,
                    now=now,
                )
            except Exception:
                log.exception("playback: compose_motion_frame failed for %s", item.id)
                return None
            self._render_image(frame)
            last = frame

            if self._stop_event.is_set() or self._pause_event.is_set():
                return last

            remaining = end_at - loop.time()
            if remaining <= 0:
                return last
            await self._wait(min(tick_period, remaining))
            if self._stop_event.is_set() or self._pause_event.is_set():
                return last
            if loop.time() >= end_at:
                return last

    async def _play_dynamic_slide_gpu(
        self, item: ContentItem
    ) -> Image.Image | None:
        """GPU path: GPUSlideCompositor lifecycle (attach → tick* →
        detach). Per-tick = one atomic ioctl with the changed plane
        properties; zero per-pixel CPU work in the inner loop.

        Transition handoff: the loop exits and we run a single
        compose_motion_frame at the slide's final state to give the
        caller's transition function a "from" frame. That final
        compose costs ~10-30 ms at 1080p, paid once per slide exit.
        We paint it to the primary plane BEFORE detaching the
        animated planes so the transition starts from the same
        pixels the user just saw on the GPU path (modulo a brief
        single-vblank period where motion text appears in both the
        primary composite and the still-attached overlay planes —
        visually the text agrees with itself, so no flicker)."""
        tz = resolve_timezone(self._get_timezone())
        total = item.duration_ms / 1000
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        end_at = t0 + total
        if slide_has_motion(item):
            tick_period = 1.0 / max(1, _FADE_FPS)
        else:
            tick_period = max(0.1, self._auto_tick)

        compositor = GPUSlideCompositor(
            item, self._renderer,
            width=self._renderer.width,
            height=self._renderer.height,
            read_asset=self._read_asset,
            cache=self._gpu_slide_cache,
        )
        compositor.attach(now=datetime.now(tz))
        try:
            while True:
                assert self._stop_event is not None
                assert self._pause_event is not None
                if self._stop_event.is_set() or self._pause_event.is_set():
                    break
                elapsed = loop.time() - t0
                now = datetime.now(tz)
                try:
                    compositor.tick(elapsed, now=now)
                except Exception:
                    log.exception(
                        "playback: GPU compositor tick failed for %s", item.id,
                    )
                    break
                if self._stop_event.is_set() or self._pause_event.is_set():
                    break
                remaining = end_at - loop.time()
                if remaining <= 0:
                    break
                await self._wait(min(tick_period, remaining))
                if self._stop_event.is_set() or self._pause_event.is_set():
                    break
                if loop.time() >= end_at:
                    break

            # Compose one final frame at the slide's final state for
            # the transition handoff. Paint to primary BEFORE detaching
            # animated planes so the transition starts from a primary
            # plane that already mirrors what the user was just seeing.
            elapsed = loop.time() - t0
            try:
                last = compose_motion_frame(
                    item, elapsed,
                    self._renderer.width, self._renderer.height,
                    read_asset=self._read_asset,
                    now=datetime.now(tz),
                )
                self._render_image(last)
                return last
            except Exception:
                log.exception(
                    "playback: GPU final-frame compose failed for %s", item.id,
                )
                return None
        finally:
            # When shader transitions are enabled, hand the compositor
            # to the loop so the next transition can keep its overlays
            # ticking through the transition window (#206). The loop
            # is responsible for eventual detach: shader-routed
            # transitions detach inside _run_shader_transition; non-
            # shader transitions detach via _drain_outgoing_compositor
            # before they fire (so overlay slot 0 is free for
            # _fade_gpu / _wipe_gpu). Otherwise (shader path off):
            # detach immediately, current behavior, no slot conflict
            # risk.
            if self._shader_transitions_enabled():
                self._outgoing_compositor = compositor
                self._outgoing_slide = item  # type: ignore[assignment]
                # Stash the slide's tick base so motion phase stays
                # continuous across the transition handoff (#207). t0
                # is the loop's monotonic time when the slide started
                # ticking; _tick_outgoing_during_transition computes
                # elapsed_s = monotonic.now() - t0 so the same phase
                # the slide had at end-of-duration carries into the
                # transition window.
                self._outgoing_slide_t0 = t0
            else:
                try:
                    compositor.detach()
                except Exception:
                    log.exception(
                        "playback: GPU compositor detach failed for %s", item.id,
                    )

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

    def _gpu_transition_slots_available(self, needed: int) -> bool:
        """True when the renderer can host `needed` overlay planes for
        a GPU-accelerated transition. Caller falls back to the
        software path when this returns False — covers MockRenderer
        (no multi-plane), DRMRenderer with max_animated_planes=0
        (existing-deployment shape), and the budget-exceeded edge."""
        if not isinstance(self._renderer, MultiPlaneRenderer):
            return False
        return getattr(self._renderer, "max_animated_planes", 0) >= needed

    def _shader_transitions_enabled(self) -> bool:
        """Feature flag for the shader compositor transition path.
        Off by default; flip OPENMARQUEE_SHADER_TRANSITIONS=1 in the
        environment to opt the welcome loop in. Designed as an env
        var (rather than settings.json) for now so operators can A/B
        the path on a single device without edits across surfaces."""
        return os.environ.get("OPENMARQUEE_SHADER_TRANSITIONS") == "1"

    def _get_or_create_shader_renderer(self) -> object | None:
        """Lazily construct the shader compositor on first transition
        that wants it. Reused for the lifetime of the loop. Returns
        None when:
          - the env flag is off,
          - a prior construction attempt already failed,
          - the renderer doesn't expose drm_fd (MockRenderer, fb0,
            DRMRenderer in pre-shared-fd build, etc.),
          - ShaderRenderer construction itself raises (libdrm/libegl
            missing on a Mac dev host, the most common case).

        On any failure we set _shader_renderer_disabled so the next
        transition doesn't pay the same cold-import cost again."""
        if self._shader_renderer is not None:
            return self._shader_renderer
        if self._shader_renderer_disabled:
            return None
        if not self._shader_transitions_enabled():
            self._shader_renderer_disabled = True
            return None
        drm_fd = getattr(self._renderer, "drm_fd", None)
        if drm_fd is None:
            log.info(
                "playback: shader transitions requested but renderer "
                "doesn't expose drm_fd; falling back to software path"
            )
            self._shader_renderer_disabled = True
            return None
        # restage_primary_fb is load-bearing for the post-transition
        # handoff: without it the shader's last fb stays on screen
        # indefinitely (kernel implicit-pin keeps it; commit() with
        # empty _pending_props is a no-op). Better to refuse the path
        # at construction time than to discover the freeze 15 s into
        # the first iris.
        if not hasattr(self._renderer, "restage_primary_fb"):
            log.warning(
                "playback: renderer exposes drm_fd but lacks "
                "restage_primary_fb; shader transitions disabled "
                "(would silently freeze the screen post-transition)"
            )
            self._shader_renderer_disabled = True
            return None
        try:
            from openmarquee.rendering.shader_compositor import ShaderRenderer
            sr = ShaderRenderer(drm_fd=drm_fd)
            sr.__enter__()
        except Exception:
            log.exception(
                "playback: ShaderRenderer construction failed; "
                "shader transitions disabled for this session"
            )
            self._shader_renderer_disabled = True
            return None
        self._shader_renderer = sr
        log.info(
            "playback: shader compositor up via shared fd=%d, %dx%d",
            drm_fd, sr.width, sr.height,
        )
        return sr

    async def _run_shader_transition(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        kind: str,
        transition_ms: int,
    ) -> bool:
        """Drive one slide-to-slide transition through the shader
        compositor. Returns True iff it ran via shader; False means
        the caller must fall back to its own (PIL) transition path.

        Shape mirrors phase7_loop_with_iris.py's transition body:
          1. Convert from/to images to RGBA bytes at renderer dims.
          2. set_kind(kind), set_from(...), set_to(...).
          3. Frame loop: set_transition_t(t in [0,1]) + commit_frame +
             cooperative-pause-aware sleep.
          4. Hand the primary plane back to multi-plane DRMRenderer in
             one atomic commit: render_frame(to_image), restage_primary
             _fb(), commit. Order matters -- without restage,
             _pending_props is empty and DRMRenderer.commit() is a
             no-op, leaving the kernel scanning shader's last fb.
        """
        if kind not in _SHADER_TRANSITION_KINDS:
            return False
        sr = self._get_or_create_shader_renderer()
        if sr is None:
            return False
        renderer = self._renderer
        width, height = sr.width, sr.height

        # If the outgoing slide had animated text layers (motion or
        # auto_mode), keep its overlays alive on multi-plane during the
        # transition so motion doesn't freeze (#206). u_from is then
        # bg+statics-ONLY of the outgoing slide -- the animated layers
        # come from live overlay planes scanned out by the HVS on top
        # of the shader's primary plane output. Without this, baking
        # animated layers' positions into u_from would double-paint
        # with the live overlays.
        outgoing = self._outgoing_compositor
        outgoing_slide = self._outgoing_slide
        from_rgba: bytes
        if outgoing is not None and outgoing_slide is not None:
            try:
                # Cached snapshot path (#205): on the second + every
                # subsequent shader transition for the same slide, this
                # is microseconds; first call composes (~600 ms at
                # 1080p) and stores. Slides with auto-mode layers skip
                # the cache and compose every time (clock text changes
                # by the second).
                from_rgba = self._snapshot_cache.get_bg_statics(
                    outgoing_slide, width, height,
                    read_asset=self._read_asset,
                )
            except Exception:
                log.exception(
                    "playback: bg+statics compose failed for outgoing "
                    "slide; falling back to full from_image (motion "
                    "will freeze through transition)"
                )
                outgoing = None  # disable the live-overlay path
                from_rgba = self._image_to_rgba_bytes(from_image, width, height)
        else:
            from_rgba = self._image_to_rgba_bytes(from_image, width, height)

        to_rgba = self._image_to_rgba_bytes(to_image, width, height)

        # Track why we exit the frame loop. Pause means a stream
        # takeover is becoming the new owner of render_frame/commit
        # (SYSTEM_SPEC §5.11) -- racing it with our handoff would
        # double-commit the primary plane. Skip the handoff in that
        # case and let the takeover own the plane. On stop or normal
        # completion the handoff is correct and required.
        import time as _time

        paused = False
        # Outgoing-compositor tick base: prefer the slide's stashed t0
        # so motion phase stays continuous across the handoff (#207).
        # Fall back to "0 = now" if the stash is missing for any
        # reason -- pulse / breathe / bounce / shake / blink are all
        # cycle-symmetric so the seam is invisible there. Ticker
        # specifically would snap back to far-right without the
        # passthrough; with it, the marquee continues from its
        # mid-scroll position.
        outgoing_t0 = self._outgoing_slide_t0 if (
            outgoing is not None and self._outgoing_slide_t0 is not None
        ) else (_time.monotonic() if outgoing is not None else None)
        try:
            sr.set_kind(kind)
            sr.set_from(from_rgba, width, height)
            sr.set_to(to_rgba, width, height)
            n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
            frame_period = (transition_ms / 1000) / n_frames
            assert self._stop_event is not None
            assert self._pause_event is not None
            for i in range(1, n_frames + 1):
                if self._pause_event.is_set():
                    paused = True
                    break
                if self._stop_event.is_set():
                    break
                t = i / n_frames
                sr.set_transition_t(t)
                sr.commit_frame()
                # Tick outgoing compositor + ramp its overlays' alpha
                # from 65535 -> 0 over t. Animation continues; the
                # fade-out smooths the transition end so slide A's
                # text doesn't snap off when we detach below.
                if outgoing is not None and outgoing_t0 is not None:
                    self._tick_outgoing_during_transition(
                        outgoing, outgoing_t0, t,
                    )
                await self._wait(frame_period)
            if not paused and not self._stop_event.is_set():
                # Land at t=1.0 so the final shader frame matches
                # to_image before we hand the plane back. Skip on
                # pause/stop -- nothing reads the result anyway.
                sr.set_transition_t(1.0)
                sr.commit_frame()
                if outgoing is not None and outgoing_t0 is not None:
                    self._tick_outgoing_during_transition(
                        outgoing, outgoing_t0, 1.0,
                    )
        except Exception:
            log.exception(
                "playback: shader transition %r failed mid-flight; "
                "primary plane will be reset to multi-plane content",
                kind,
            )
            # Fall through to the handoff dance anyway (unless paused)
            # so the screen recovers cleanly after a shader-side error.

        # Detach the outgoing compositor now -- it's done its job (kept
        # motion alive through the transition), and its overlay slots
        # need to be free for the next slide's GPUSlideCompositor.attach.
        # Done BEFORE the primary handoff so the kernel doesn't have to
        # honor a queue of overlay-property atomic commits that race the
        # primary FB_ID swap.
        if outgoing is not None:
            self._drain_outgoing_compositor()

        if paused:
            # Stream takeover owns the plane now. Don't fight it.
            return True

        # Hand the primary plane back to multi-plane DRMRenderer.
        # render_frame paints the dumb buffer, restage_primary_fb
        # stages the FB_ID + CRTC rects (otherwise commit() is a
        # no-op when _pending_props is empty), commit atomically
        # rebinds primary to OUR fb in one vblank.
        # restage_primary_fb's existence is checked at construction
        # time in _get_or_create_shader_renderer; if it's gone now,
        # something is very wrong (renderer torn down mid-transition?).
        try:
            renderer.render_frame(to_image.convert("RGB").tobytes())
            renderer.restage_primary_fb()
            renderer.commit()
        except Exception:
            log.exception(
                "playback: post-shader-transition handoff to "
                "multi-plane failed; screen may be stuck on shader's "
                "last frame until the next slide attach"
            )
        return True

    def _image_to_rgba_bytes(
        self, image: Image.Image, width: int, height: int,
    ) -> bytes:
        """Resize + RGBA-convert a PIL image to width*height*4 bytes."""
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        if image.size != (width, height):
            image = image.resize((width, height), Image.NEAREST)
        return image.tobytes()

    def _tick_outgoing_during_transition(
        self,
        compositor: "GPUSlideCompositor",
        t0_monotonic: float,
        transition_t: float,
    ) -> None:
        """Per-shader-frame tick for the outgoing slide's compositor
        during a transition (#206). Calls compositor.tick(elapsed) so
        motion phase keeps advancing, then ramps every active overlay
        plane's alpha from 65535 (full) at transition_t=0 to 0 (gone)
        at transition_t=1. The HVS composites each overlay over the
        shader's primary-plane output at scanout; the ramp gives a
        smooth fade-out instead of a snap-off when we detach at the
        end of _run_shader_transition.

        Tick exceptions are logged but non-fatal -- a one-frame motion
        glitch is preferable to a crashed transition mid-flight.

        Note: alpha-ramp runs AFTER compositor.tick() and clobbers any
        per-frame alpha that pulse/blink motion staged on the same
        slots. Last-write-wins is correct for the transition: we want
        a smooth monotonic fade-out, not pulse-during-fade. For
        layers with non-pulse motion the alpha override is invisible.
        """
        import time as _time
        from datetime import UTC
        elapsed = _time.monotonic() - t0_monotonic
        try:
            compositor.tick(elapsed, now=datetime.now(UTC))
        except Exception:
            log.exception(
                "playback: outgoing-compositor tick during transition failed"
            )
        # Now ramp alpha. Reach into the compositor's slot mapping;
        # _slot_for_layer is module-internal but the shape is stable
        # (private inside the gpu_compositor.py module, fine for our
        # adjacent module). Update each animated plane's alpha + commit.
        try:
            alpha = max(0, min(65535, int(round(65535 * (1.0 - transition_t)))))
            renderer = self._renderer
            for slot_idx in compositor._slot_for_layer.values():
                renderer.update_animated_layer(slot_idx, alpha=alpha)
            renderer.commit()
        except Exception:
            log.exception(
                "playback: outgoing-compositor alpha ramp during transition failed"
            )

    async def _fade(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Alpha-blend from `from_image` to `to_image` over `transition_ms`.

        Routes through `_fade_gpu` when the renderer is multi-plane —
        animating an overlay plane's `alpha` property gets the HVS
        doing the blend at scanout (zero per-pixel CPU per frame).
        Falls back to PIL Image.blend when the renderer can't host
        an overlay (MockRenderer, LED-matrix renderers, DRMRenderer
        constructed with max_animated_planes=0).

        Returns early on stop OR pause request — pause-awareness keeps
        the transition from painting playlist frames over an in-flight
        stream takeover.
        """
        if self._gpu_transition_slots_available(1):
            try:
                await self._fade_gpu(from_image, to_image, transition_ms)
                return
            except Exception:
                log.exception(
                    "playback: GPU fade failed, falling back to software",
                )
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

    async def _fade_gpu(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """GPU-accelerated fade via overlay plane.alpha.

        from_image is already on the primary plane (the prior slide's
        final compose painted it there). We attach to_image as a
        fullscreen overlay at alpha=0, animate alpha 0→65535 over
        transition_ms via atomic-commit per frame, then paint
        to_image to primary + detach the overlay so the next slide's
        attach starts from a clean slot 0 and the right primary
        content. Per-frame is one ioctl; HVS does the blend. The
        software path's PIL Image.blend at 1080p was 140-180 ms/frame
        (~5-7 fps); this should hit a clean 30 fps."""
        renderer = self._renderer
        width, height = renderer.width, renderer.height

        # Coerce to_image to RGBA at the renderer's native dims so the
        # plane fb upload matches the dumb buffer's row pitch.
        if to_image.mode != "RGBA":
            to_image = to_image.convert("RGBA")
        if to_image.size != (width, height):
            to_image = to_image.resize((width, height), Image.NEAREST)
        rgba_bytes = to_image.tobytes()

        # Attach overlay slot 0 fullscreen, alpha=0 (invisible). Then
        # one commit so the modeset takes effect before the alpha
        # ramp begins.
        renderer.attach_animated_layer(
            0, rgba_bytes,
            src_w=width, src_h=height,
            crtc_x=0, crtc_y=0,
            crtc_w=width, crtc_h=height,
        )
        renderer.update_animated_layer(0, alpha=0)
        renderer.commit()

        try:
            n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
            frame_period = (transition_ms / 1000) / n_frames
            for i in range(1, n_frames + 1):
                assert self._stop_event is not None
                assert self._pause_event is not None
                if self._stop_event.is_set() or self._pause_event.is_set():
                    break
                alpha = max(0, min(65535, int(round(65535 * i / n_frames))))
                renderer.update_animated_layer(0, alpha=alpha)
                renderer.commit()
                await self._wait(frame_period)
            # Land at full alpha so the screen content matches
            # to_image perfectly before we hand off.
            renderer.update_animated_layer(0, alpha=65535)
            renderer.commit()
            # Paint to_image into the primary plane so when we detach
            # the overlay, primary already has the same content — no
            # visible flash. Convert back to RGB for render_frame.
            self._render_image(to_image.convert("RGB"))
        finally:
            renderer.detach_animated_layer(0)
            renderer.commit()

    async def _slide(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Push transition: `from_image` slides off to the left while
        `to_image` slides in from the right at the same rate. Distinct
        from wipe in that BOTH frames move (rather than to_image
        revealing under a stationary from_image).

        Routes through the shader compositor when available."""
        if await self._run_shader_transition(
            from_image, to_image, "slide", transition_ms,
        ):
            return
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
        from fade and wipe at small panel sizes that it stays legible.

        Routes through the shader compositor when available (env flag
        OPENMARQUEE_SHADER_TRANSITIONS=1 + renderer.drm_fd present).
        That hits 30 fps stable on Pi Zero 2 W at 1080p; the PIL
        software path below is the fallback for non-DRM renderers
        (LED-matrix, fb0, MockRenderer) and dev hosts."""
        if await self._run_shader_transition(
            from_image, to_image, "iris", transition_ms,
        ):
            return
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
        and ticker-style HUB75 strips.

        Routes through the shader compositor when available."""
        if await self._run_shader_transition(
            from_image, to_image, "scroll", transition_ms,
        ):
            return
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

        Routes through the shader compositor when available.
        """
        if await self._run_shader_transition(
            from_image, to_image, "flip", transition_ms,
        ):
            return
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

        Routes through the shader compositor when available.
        """
        if await self._run_shader_transition(
            from_image, to_image, "marquee", transition_ms,
        ):
            return
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

        Routes through the shader compositor when available. Hashes
        v_uv per fragment for the threshold (deterministic per pixel,
        same shape as the np.random thresholds below — different RNG,
        same visual class).
        """
        if await self._run_shader_transition(
            from_image, to_image, "dissolve", transition_ms,
        ):
            return
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

        Routes through the shader compositor when available. The
        shader version uses a UV-space block-quantize on both samplers
        with a wave that peaks at t=0.5 — same visual class as the
        PIL path's PIL.Image.resize(NEAREST) chunk-blur.
        """
        if await self._run_shader_transition(
            from_image, to_image, "pixelate", transition_ms,
        ):
            return
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

        Routes through the shader compositor when available.
        """
        if await self._run_shader_transition(
            from_image, to_image, "halftone", transition_ms,
        ):
            return
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

        Routes through the shader compositor when available. The
        shader version uses smoothstep around the sweep line for the
        glow band — slightly softer than the PIL path's solid white
        rectangle but reads as the same CRT-beam look.
        """
        if await self._run_shader_transition(
            from_image, to_image, "scanline", transition_ms,
        ):
            return
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

        Routes through the shader compositor when available.
        """
        if await self._run_shader_transition(
            from_image, to_image, "glitch", transition_ms,
        ):
            return
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

    async def _push(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Classic-display push: to_image enters from the LEFT, pushing
        from_image off the right edge. Both images move together at the
        same rate (no gap). A 1-px bright vertical separator paints at
        the seam between them — gives the mechanical-projector "blade"
        feel that distinguishes push from `_slide`'s gap-less but
        bare-edge motion.

        Direction (left-entry) is the mirror of `_slide` (right-entry),
        so the operator-visible difference between the two pulldown
        choices is direction PLUS the projector-blade separator. First
        of the classic-display family per the 2026-04-28 palette spec.

        Strip-graceful: width<2 -> fall back to fade. Same shape as
        flip/marquee/pixelate strip fallbacks.

        Routes through the shader compositor when available.
        """
        if await self._run_shader_transition(
            from_image, to_image, "push", transition_ms,
        ):
            return
        from PIL import ImageDraw

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
            offset = max(0, min(width, int(round(width * progress))))
            frame = Image.new("RGB", (width, height))
            # to_image enters from left: source columns [width-offset,
            # width) go to frame columns [0, offset). I.e. to_image is
            # translated to x = -width + offset, so its rightmost
            # `offset` columns are visible at the left.
            if offset > 0:
                frame.paste(
                    to_image.crop((width - offset, 0, width, height)), (0, 0)
                )
            # from_image exits to right: source columns [0, width-offset)
            # go to frame columns [offset, width). I.e. from_image is
            # translated to x = +offset, its leftmost `width-offset`
            # columns visible.
            if offset < width:
                frame.paste(
                    from_image.crop((0, 0, width - offset, height)),
                    (offset, 0),
                )
            # Bright projector-blade separator at the seam (column
            # offset). Skipped at offset=0 and offset=width (no seam
            # visible — full from or full to). 1px wide for legibility
            # on small panels; reads as a clean cut on bigger ones.
            if 0 < offset < width:
                ImageDraw.Draw(frame).rectangle(
                    (offset - 1, 0, offset - 1, height - 1),
                    fill=(255, 255, 255),
                )
            self._render_image(frame)
            await self._wait(frame_period)

    async def _blinds(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Venetian-blind reveal: horizontal slats open to expose
        to_image. Each slat has its own midline; the visible band
        within each slat grows from a horizontal hairline at the
        midline outward, until at progress=1 every slat is fully open
        and the entire to_image is revealed.

        n_slats = max(2, height // 8) — slats are roughly 8px tall,
        clamped to at least two so the blind effect reads as a blind
        even on small panels. Same Image.composite-mask pattern that
        _halftone uses, just with rectangles instead of ellipses.

        Strip-graceful: height<4 leaves no room for two slats with
        meaningful midline-spread bands, so delegate to fade.

        Routes through the shader compositor when available.
        """
        if await self._run_shader_transition(
            from_image, to_image, "blinds", transition_ms,
        ):
            return
        from PIL import ImageDraw

        width, height = from_image.size
        if height < 4:
            await self._fade(from_image, to_image, transition_ms)
            return

        n_slats = max(2, height // 8)

        n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
        frame_period = (transition_ms / 1000) / n_frames
        for i in range(1, n_frames + 1):
            assert self._stop_event is not None
            assert self._pause_event is not None
            if self._stop_event.is_set() or self._pause_event.is_set():
                return
            progress = i / n_frames
            mask = Image.new("L", (width, height), 0)
            draw = ImageDraw.Draw(mask)
            # Per-slat midline-out reveal. Use float arithmetic for the
            # slat boundaries so non-integer division (e.g. 32 / 5 =
            # 6.4 px slats) doesn't accumulate rounding error across
            # slats — each pair of integer top/bot rounds back from
            # the floating boundary.
            slat_h = height / n_slats
            for s in range(n_slats):
                slat_top = int(round(s * slat_h))
                slat_bot = int(round((s + 1) * slat_h))
                slat_height = slat_bot - slat_top
                band_height = int(round(slat_height * progress))
                if band_height <= 0:
                    continue
                band_top = slat_top + (slat_height - band_height) // 2
                band_bot = band_top + band_height
                draw.rectangle(
                    (0, band_top, width - 1, band_bot - 1), fill=255
                )
            frame = Image.composite(to_image, from_image, mask)
            self._render_image(frame)
            await self._wait(frame_period)

    async def _shutter(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Hexagonal-aperture shutter: a 6-sided regular polygon centered
        on the canvas grows from a point at progress=0 to fully covering
        the canvas at progress=1. Inside the polygon = to_image; outside
        = from_image. Distinct from `_iris` (circle, rotation-symmetric)
        by the polygon's six straight edges — at mid-transition the
        hexagon's vertices reach further than its edge-midpoints, giving
        the aperture-blade silhouette the operator expects from a
        camera-shutter UI.

        Closes the classic-display family AND the 16-transition palette
        batch per the 2026-04-28 spec.

        Strip-graceful: width<4 or height<4 leaves no room for the
        hexagon to read as anything other than a stripe (six vertices
        overlap at low resolution), so delegate to fade. Same shape as
        halftone's strip fallback.

        Routes through the shader compositor when available.
        """
        if await self._run_shader_transition(
            from_image, to_image, "shutter", transition_ms,
        ):
            return
        from math import cos, pi, sin

        from PIL import ImageDraw

        width, height = from_image.size
        if width < 4 or height < 4:
            await self._fade(from_image, to_image, transition_ms)
            return

        cx, cy = width / 2.0, height / 2.0
        # Max vertex radius for full canvas coverage at progress=1.
        # The hexagon's tightest direction (edge midpoint, angle π/6
        # from a vertex) sits at R·cos(π/6) from center — only ~0.866R
        # — so a vertex radius equal to the corner distance leaves a
        # sliver of from_image at every canvas corner that lands
        # between vertex angles. Dividing by cos(π/6) = √3/2 inflates
        # the vertex radius enough that the hexagon's narrowest
        # direction still reaches the canvas corner. +2px safety
        # margin handles per-frame rounding.
        corner_dist = (cx * cx + cy * cy) ** 0.5
        max_r = (corner_dist + 2.0) / cos(pi / 6)

        # Vertex angles (regular hexagon, "pointy-right" orientation —
        # vertex at angle 0 points along +x).
        vertex_angles = [pi / 3.0 * k for k in range(6)]

        n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
        frame_period = (transition_ms / 1000) / n_frames
        for i in range(1, n_frames + 1):
            assert self._stop_event is not None
            assert self._pause_event is not None
            if self._stop_event.is_set() or self._pause_event.is_set():
                return
            progress = i / n_frames
            radius = progress * max_r
            vertices = [
                (cx + radius * cos(a), cy + radius * sin(a))
                for a in vertex_angles
            ]
            mask = Image.new("L", (width, height), 0)
            ImageDraw.Draw(mask).polygon(vertices, fill=255)
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

        Routes through `_wipe_gpu` when the renderer is multi-plane —
        an overlay plane with growing CRTC_W gets HVS-clipped at scan-
        out (zero per-pixel CPU). Falls back to PIL paste-and-crop
        otherwise.

        Returns early on stop. Same frame cadence as _fade so the two
        transitions feel like the same smoothness at equal transition_ms.
        """
        if self._gpu_transition_slots_available(1):
            try:
                await self._wipe_gpu(from_image, to_image, transition_ms)
                return
            except Exception:
                log.exception(
                    "playback: GPU wipe failed, falling back to software",
                )
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

    async def _wipe_gpu(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """GPU-accelerated wipe via overlay CRTC_W animation.

        from_image stays on primary; to_image goes onto overlay slot
        0 with full alpha. Each frame, the overlay's SRC_W + CRTC_W
        animate 1→width so the overlay reveals the to_image from the
        left edge. SRC_X / CRTC_X stay 0 (left-anchored). HVS does
        the clip at scanout — zero per-pixel CPU per frame.

        Same paint-to-primary-then-detach landing as _fade_gpu so the
        next slide's attach starts clean."""
        renderer = self._renderer
        width, height = renderer.width, renderer.height

        if to_image.mode != "RGBA":
            to_image = to_image.convert("RGBA")
        if to_image.size != (width, height):
            to_image = to_image.resize((width, height), Image.NEAREST)
        rgba_bytes = to_image.tobytes()

        # Attach overlay covering only the leftmost 1px initially.
        # vc4 doesn't accept CRTC_W=0; starting at 1 is safe and the
        # 1-pixel wide stripe is invisible at the first frame.
        # Critical invariant: src_w == crtc_w throughout the ramp so
        # the vc4 HVS scaler is NEVER engaged. Equal dims = 1:1
        # blit, which sidesteps vc4's scaler-minimum LBM constraints
        # (which can floor at 8-16 px). If a future refactor splits
        # them (e.g. fixed src_w + animating crtc_w to scale-reveal),
        # the minimum needs revisiting.
        renderer.attach_animated_layer(
            0, rgba_bytes,
            src_w=width, src_h=height,
            crtc_x=0, crtc_y=0,
            crtc_w=1, crtc_h=height,
        )
        renderer.update_animated_layer(0, src_w=1)
        renderer.commit()

        try:
            n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
            frame_period = (transition_ms / 1000) / n_frames
            for i in range(1, n_frames + 1):
                assert self._stop_event is not None
                assert self._pause_event is not None
                if self._stop_event.is_set() or self._pause_event.is_set():
                    break
                split = max(1, min(width, int(round(width * i / n_frames))))
                renderer.update_animated_layer(0, src_w=split, crtc_w=split)
                renderer.commit()
                await self._wait(frame_period)
            # Land at full width so the screen exactly matches
            # to_image before primary takes over.
            renderer.update_animated_layer(0, src_w=width, crtc_w=width)
            renderer.commit()
            self._render_image(to_image.convert("RGB"))
        finally:
            renderer.detach_animated_layer(0)
            renderer.commit()


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
