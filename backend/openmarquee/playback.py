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
import math
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
    _effect_freq,
    compose_motion_frame,
    compute_phase,
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
# software path. All 14 kinds in qarl's transition palette go
# through here -- fade and wipe used to have their own
# plane-property animation paths (_fade_gpu / _wipe_gpu) but those
# were deleted in the cleanup that unified all transitions on the
# shader path so motion-through-transitions (#206) works
# uniformly across every kind.
_SHADER_TRANSITION_KINDS = frozenset({
    "fade",
    "wipe",
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
    from openmarquee.content import TextLayer, TextSlide
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
        self._shader_renderer: ShaderRenderer | None = None
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
        # if the shader path turned out to be unavailable.
        #
        # The outgoing compositor owns its overlay slots [0..N-1] until
        # drain. Future symmetric "fade-in for incoming compositor"
        # work must NOT pre-attach the next compositor before this one
        # drains -- they'd collide on slot indices. Either drain first
        # (sacrifice continuous incoming-side motion) or split the
        # plane budget statically (incoming gets [N/2..N-1]).
        self._outgoing_compositor: GPUSlideCompositor | None = None
        self._outgoing_slide: TextSlide | None = None
        # Incoming slide reference set by the dispatcher BEFORE the
        # transition method fires; consumed by _run_shader_transition
        # to compose u_to as bg+statics-only (excluding animated
        # layers, parallel to u_from). Without this, slide B's
        # animated text would appear as a frozen ghost in the
        # iris-revealed area of the shader output -- overlapping with
        # slide A's live-moving ticker on the overlay plane until the
        # incoming compositor attaches after the transition.
        self._incoming_slide: TextSlide | None = None
        # Last steady-tick elapsed for the outgoing slide (#218).
        # Stashed in _play_dynamic_slide_gpu's finally as the elapsed
        # at slide-end. _run_shader_transition uses this as the
        # OUTGOING ANCHOR: shader-anim's first frame stages the ticker
        # at this elapsed, then advances naturally. The "lost time"
        # between last steady tick and first shader frame (texture
        # upload + drain) is FROZEN -- the clock pauses during the
        # gap, so ticker doesn't snap forward when the screen
        # un-freezes.
        self._outgoing_slide_last_elapsed: float | None = None
        # Frozen elapsed for the incoming slide at the moment shader-
        # anim stops painting (#217 v2). Captured at end of frame loop
        # in _run_shader_transition. _play_dynamic_slide_gpu reads it
        # to set compositor's t0 such that the FIRST tick fires at
        # this exact elapsed -- ticker resumes from shader-anim's last
        # painted position with no jump, even though the compositor
        # .attach gap freeze duration is variable. The clock pauses
        # during the gap; subsequent ticks advance from the freeze
        # point.
        self._incoming_slide_freeze_elapsed: float | None = None
        # Background prerender tasks for the shader-transition snapshot
        # cache (#216). Each task warms bg_statics + anim_layer for one
        # slide via asyncio.to_thread during the slide's display window
        # so the upcoming transition pays no rasterize cost. Tracked so
        # stop() can cancel them; tasks self-discard from the set on
        # completion via add_done_callback.
        self._prerender_tasks: set[asyncio.Task] = set()
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
        # Slot-start monotonic timestamp -- stamped by the play methods
        # at t0 so capture_current_frame() can compute elapsed_s from
        # the same clock the live render uses. None when no slot is
        # active. Added 2026-05-06 for /api/playback/current-frame.
        self._slot_t0: float | None = None
        # In-memory cache for /api/playback/current-frame: tuple of
        # (png_bytes, captured_at_monotonic, captured_playlist_id).
        # Cache hit when both (a) age < 5 minutes AND (b) playlist
        # hasn't changed. None until first capture. Lock prevents two
        # concurrent captures racing the readback path.
        self._frame_cache: tuple[bytes, float, UUID | None] | None = None
        self._frame_capture_lock: asyncio.Lock | None = None
        # Bug 8 / Fix B (2026-05-17): per-slide IPC-failure throttle.
        # Without this, a permanently-broken slide (e.g. multi-trak
        # MP4 that the rust sidecar can't demux) makes the catch-and-
        # continue path log ERROR + full traceback at ~3.4 Hz, spamming
        # journalctl + spinning disk IO. First failure per slide_id
        # still logs ERROR with traceback (operator must see the
        # cause); subsequent failures for the SAME id log DEBUG one-
        # liner only. Reset on playlist-id change so an operator who
        # fixes the bad slide and switches playlists gets a fresh
        # ERROR if the fix didn't take.
        self._failed_slide_ids: set[UUID] = set()

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
        # Eager-warm the shader renderer (#217 follow-on). 5-6s of
        # GL/EGL/GBM init + 15-shader compile happens once at loop
        # start instead of stalling the FIRST shader transition mid-
        # playback. After this, every transition gets sr=0ms init
        # cost. Synchronous on the asyncio main thread is fine here:
        # it runs ONCE before the loop starts iterating, and the
        # primary plane keeps showing whatever was last on it
        # (welcome-loop frame, stream takeover frame, etc.) until
        # the loop's first slide attaches.
        if self._shader_transitions_enabled():
            try:
                self._get_or_create_shader_renderer()
            except Exception:
                log.exception(
                    "playback: eager shader-renderer warmup at start "
                    "failed; falling back to lazy init at first transition",
                )
        self._task = asyncio.create_task(self._loop())
        # 2026-05-17 frozen-sign incident on 192.168.1.67: an unhandled
        # exception inside _loop() silently killed the playback task,
        # leaving the sign on the boot slide with ZERO log surface.
        # asyncio.create_task swallows exceptions until the task is
        # awaited or GC'd. Surface them at task-done time so future
        # crashes hit journalctl instead of freezing the sign blind.
        self._task.add_done_callback(self._on_loop_task_done)

    @staticmethod
    def _on_loop_task_done(task: "asyncio.Task[None]") -> None:
        """Surface _loop task exceptions via the logger. Without this,
        a silent crash leaves the sign frozen with no diagnostic
        breadcrumb. Cancellation is the normal stop path (stop() awaits
        the task after setting _stop_event), so log it at INFO only."""
        if task.cancelled():
            log.info("playback: _loop task cancelled (normal stop path)")
            return
        exc = task.exception()
        if exc is not None:
            log.error(
                "playback: _loop task crashed -- sign will be frozen "
                "until restart. Cause: %r", exc, exc_info=exc,
            )

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
            self._slot_t0 = None
            # Drop the lock so the next start() rebinds against the
            # fresh event loop. Without this, tests that run multiple
            # asyncio.run() rounds against the same PlaybackLoop hit
            # "RuntimeError: ... attached to a different loop".
            self._frame_capture_lock = None
            # Cancel any in-flight prerender tasks (#216). Cancel is
            # best-effort: PIL rasterize is CPU-bound and cancel() can
            # only fire when the thread checks; in practice the task
            # finishes the current rasterize (~100ms) and exits. We
            # do NOT await them here -- letting them complete in the
            # background while the loop tears down keeps stop()
            # responsive. They write to the cache which gets cleared
            # below; any race writes are wasted but not corrupt.
            for task in list(self._prerender_tasks):
                task.cancel()
            self._prerender_tasks.clear()
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
            self._incoming_slide_freeze_elapsed = None
            self._drain_outgoing_compositor()

    def _schedule_prerender(self, item: ContentItem | None) -> None:
        """Fire-and-forget background warm of `item`'s shader-transition
        snapshot cache (#216). Runs PIL bg+statics composite + first-
        animated-layer rasterize off the event loop via
        asyncio.to_thread, so by the time the slide is the outgoing /
        incoming side of a transition the cache is already populated
        and the transition starts with no rasterize stall.

        Skips:
          - shader path disabled (no env flag, or test renderers
            without drm_fd). The cache isn't read in that world, so
            prerender is pure waste, AND keeps the test event loop
            free of background to_thread activity that would
            destabilize timing-sensitive playback tests.
          - non-text-slide items (image/video have no animated layers
            and no compose_slide_bg_statics_rgba path).

        Auto-mode slides skip cache (recompute every transition since
        clock text changes), so the prerender does work that gets
        discarded. Acceptable -- auto slides are rarely the source of
        real stall since their compose path was already paid by the
        steady-state attach."""
        if not self._shader_transitions_enabled():
            return
        if item is None or getattr(item, "type", None) != "text_slide":
            return
        renderer = self._renderer
        if not hasattr(renderer, "width") or not hasattr(renderer, "height"):
            return
        width = renderer.width
        height = renderer.height
        cache = self._snapshot_cache
        read_asset = self._read_asset
        # Stringify the slide id so the pool key matches what
        # GPUSlideCompositor uses (`str(slide.id)`) at attach time --
        # otherwise UUID vs str mismatch silently misses the pool.
        raw_id = getattr(item, "id", None)
        slide_id = str(raw_id) if raw_id is not None else None
        # If renderer supports the per-slide primary buffer pool
        # (#218 part 2 - DRMRenderer in shader-transition mode), also
        # warm the slide's dedicated primary fb in the same to_thread
        # task. By the time this slide's compositor.attach runs, the
        # buffer is already painted; attach just flips FB_ID atomically.
        # ZERO memcpy on the seam.
        supports_pool = (
            slide_id is not None
            and hasattr(renderer, "prepare_primary_buffer")
        )

        async def _run() -> None:
            try:
                await asyncio.to_thread(
                    cache.prerender_for_transition,
                    item, width, height, read_asset=read_asset,
                )
                if supports_pool:
                    # Snapshot cache now has bg+statics RGBA. Convert
                    # to RGB and pre-paint the slide's pool buffer.
                    # All on this worker thread so the asyncio main
                    # thread stays free. content_version = updated_at
                    # so an edit to the slide invalidates the pool's
                    # cached pixels and we repaint in place (#218
                    # SHOULD-FIX from pre-commit review).
                    try:
                        rgba = cache.get_bg_statics(
                            item, width, height, read_asset=read_asset,
                        )
                        rgb = (
                            Image.frombytes("RGBA", (width, height), rgba)
                            .convert("RGB")
                            .tobytes()
                        )
                        await asyncio.to_thread(
                            renderer.prepare_primary_buffer,
                            slide_id, rgb,
                            content_version=getattr(item, "updated_at", None),
                        )
                    except Exception:
                        log.exception(
                            "playback: pool buffer prerender failed "
                            "for slide %s", slide_id,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "playback: prerender task crashed for slide %s",
                    getattr(item, "id", "<no-id>"),
                )

        task = asyncio.create_task(_run())
        self._prerender_tasks.add(task)
        task.add_done_callback(self._prerender_tasks.discard)

    def _evict_caches_to_window(self, keep_ids: set[UUID]) -> None:
        """Drop both the snapshot and asset caches' entries whose
        slide_id isn't in `keep_ids`. The play loop calls this once
        per iteration with {current, next} so the caches stay bounded
        on a circular playlist (where plain LRU is 0% hit-rate).

        Coordinates with DRMRenderer's per-slide primary buffer pool:
        SlideAssetCache.evict_except passes the renderer through so
        the kernel-side dumb buffer for an evicted slide gets released
        too -- otherwise userspace shrinks but kernel memory stays
        held.
        """
        try:
            self._snapshot_cache.evict_except(keep_ids)
        except Exception:
            log.exception("playback: snapshot cache eviction failed")
        try:
            self._gpu_slide_cache.evict_except(
                keep_ids, renderer=self._renderer,
            )
        except Exception:
            log.exception("playback: asset cache eviction failed")

    def _drain_outgoing_compositor(self) -> None:
        """Detach the outgoing slide's compositor (#206 cleanup) if one
        is being held alive across a transition. Idempotent. Called
        AFTER any transition that didn't claim the compositor itself
        and on stop(). With every transition kind now shader-routed,
        the BEFORE-transition drain that used to fire for
        non-shader transitions is no longer needed."""
        c = self._outgoing_compositor
        self._outgoing_compositor = None
        self._outgoing_slide = None
        self._outgoing_slide_last_elapsed = None
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
                self._slot_t0 = None
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
                # Stamp slot t0 here so EVERY slot (static image,
                # static text, dynamic text, video) has a correct
                # baseline for capture_current_frame's elapsed_s.
                # The dynamic-text play methods overwrite this after
                # their own freeze-aware t0 calc, which is fine -- by
                # then their override IS the authoritative one.
                self._slot_t0 = asyncio.get_event_loop().time()
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

                # Background prerender (#216): warm the snapshot cache
                # for both the CURRENT item (it'll be the outgoing side
                # in ~duration_ms) and the NEXT item (it'll be the
                # incoming side at the same time). Both rasterizes
                # finish well within the duration_ms window typically
                # used (>=1s), so transition start pays no PIL cost.
                # Schedules via asyncio.to_thread so the rasterize
                # runs off the event loop.
                self._schedule_prerender(item)
                next_item = items[(i + 1) % len(items)] if len(items) > 1 else None
                if next_item is not None:
                    self._schedule_prerender(next_item)

                # Cycle-aware cache eviction (2026-05-06 OOM fix). A
                # circular playlist's working set is "all of it," so
                # plain LRU has 0% hit rate when the cache size is
                # below playlist size -- the wrap-around always cold-
                # misses. The right bound is "current + next-prefetched
                # only"; everything else is dead weight at ~12 MB
                # userspace per slide × 32 slides => 400 MB on a 416 MB
                # Pi Zero 2 W. Done here (after prerender scheduling)
                # so the prerender's cache writes for current + next
                # don't get evicted by the same call. Per-iteration
                # rather than per-transition so a schedule-rule mid-
                # cycle naturally drops the prior playlist's stale
                # prerender.
                keep_ids: set[UUID] = {item.id}
                if next_item is not None:
                    keep_ids.add(next_item.id)
                self._evict_caches_to_window(keep_ids)

                # Slice 4 gate: when the renderer is the Rust IPC
                # sidecar proxy (or AutoFallbackRenderer wrapping one),
                # the per-tick PIL composite + render_frame(bytes) hot
                # path doesn't fit -- the sidecar owns rasterization
                # AND DRM page-flip. Drive it via begin_slide + advance
                # IPC ops instead.
                #
                # Slice-4-followup (rust transitions): after the current
                # slide's duration loop ends, drive begin_transition +
                # advance through the transition window into the next
                # slide -- the sidecar's state machine internally
                # promotes from PaintTransition to PaintSlide(next), so
                # the next iteration's begin_slide just re-anchors the
                # to-slide's clock (idempotent visible-state-wise).
                if self._renderer_supports_ipc_ops():
                    # Compute next item for the transition handoff.
                    # Playlist wraps: at end-of-list, transition into
                    # items[0] to mirror the existing PIL transition
                    # path's behavior.
                    next_for_transition = (
                        items[(i + 1) % len(items)] if len(items) > 1 else None
                    )
                    # 2026-05-17 frozen-sign guard. The TODO at
                    # _play_via_rust_ipc's docstring (L1004) flagged
                    # that RustRendererRespawnedError /
                    # AutoFallbackInMockError / generally any non-
                    # Unsupported* IPC error propagates uncaught and
                    # kills the outer _loop. That's exactly what
                    # happened on 192.168.1.67: slide 0 begin_slide
                    # fired, an advance op (or its response) blew up,
                    # the task died silently, sign stayed frozen on
                    # boot slide for 10+ min with zero log surface.
                    # Catch broadly here so a single-slide IPC fault
                    # doesn't take the whole sign down; log full
                    # traceback so the journal carries the diagnosis.
                    try:
                        rendered = await self._play_via_rust_ipc(
                            item,
                            next_item=next_for_transition,
                            transition_kind=(item.transition or "cut"),
                            transition_ms=int(item.transition_ms or 0),
                        )
                    except Exception as e:
                        # Bug 8 / Fix B: per-slide throttle. First fail
                        # per id logs ERROR with traceback; subsequent
                        # fails for the SAME id log DEBUG only. Avoids
                        # journal-spam when a bad slide sits in a
                        # 1-item playlist (frozen-sign incident @
                        # 192.168.1.67 hot-spun at 3.4 Hz with full
                        # tracebacks until ce225f3 + Fix A landed).
                        if item.id in self._failed_slide_ids:
                            log.debug(
                                "playback: IPC playback failed for "
                                "slide id=%s (throttled; first fail "
                                "carried the traceback): %s",
                                item.id, e,
                            )
                        else:
                            self._failed_slide_ids.add(item.id)
                            log.exception(
                                "playback: IPC playback failed for slide "
                                "id=%s type=%s; advancing to next item "
                                "(subsequent fails for this id will be "
                                "throttled to DEBUG)",
                                item.id, item.type,
                            )
                        # Brief settle so a hot-loop of failing slides
                        # doesn't burn CPU. 250ms is short enough that
                        # one bad slide adds barely-visible delay,
                        # long enough to avoid a tight crash-loop.
                        await self._wait(0.25)
                        continue
                    if self._stop_event.is_set():
                        break
                    if self._pause_event.is_set():
                        self._resume_at_index = i
                        break
                    # Whether the slide rendered or was skipped (e.g.,
                    # VideoSlide on a video-less sidecar build), move
                    # to the next item. `rendered=False` means
                    # UnsupportedSlideError fired and was logged by
                    # _play_via_rust_ipc; the sidecar is healthy.
                    _ = rendered
                    continue

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
                        # Stash next_item so _run_shader_transition can
                        # compose u_to as bg+statics-only (parallel to
                        # u_from). TextSlides only -- ImageSlide /
                        # VideoSlide don't have animated layers to skip
                        # so their full composite IS bg+statics anyway.
                        self._incoming_slide = (
                            next_item if next_item.type == "text_slide"
                            else None
                        )
                        # Every transition kind is shader-routed since
                        # the unification cleanup. Shader path drains
                        # outgoing internally; if shader is unavailable
                        # (env off, dev host without libdrm, etc.)
                        # _run_shader_transition drains BEFORE
                        # returning False so the PIL fallback below
                        # sees clean overlay slots.
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
                # Same housekeeping for the incoming-slide stash --
                # _run_shader_transition reads it on entry; clear it
                # so a future transition without a known incoming
                # slide doesn't stale-read a previous next_item.
                self._incoming_slide = None

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

    def _renderer_supports_ipc_ops(self) -> bool:
        """True when the injected renderer drives the Rust IPC sidecar
        (RustRenderer or AutoFallbackRenderer wrapping one).

        Gates the slice-4 Rust route. Duck-typed on `begin_slide` +
        `advance` so a test stub doesn't need to be a real RustRenderer
        subclass. False for DRMRenderer / MockRenderer / LED-matrix
        adapters -- those paths stay on the existing PIL hot path."""
        return (
            hasattr(self._renderer, "begin_slide")
            and hasattr(self._renderer, "advance")
        )

    async def _play_via_rust_ipc(
        self,
        item: ContentItem,
        *,
        next_item: ContentItem | None = None,
        transition_kind: str = "cut",
        transition_ms: int = 0,
    ) -> bool:
        """Drive one slide through the Rust IPC sidecar's begin_slide
        + advance contract, AND drive begin_transition into the next
        slide if one is specified. The sidecar owns rasterization +
        DRM page-flip; Python sends ops and waits for SlideComplete
        + the transition state machine to promote into the to-slide.

        Returns True when the slide played out normally. Returns False
        when the slide kind isn't supported by the sidecar yet (today:
        VideoSlide -- task #76 wires V4L2). Caller advances to the
        next item on False.

        Zero PIL invocation on the hot path: bytes never cross the IPC
        boundary; the sidecar paints into its own EGL session and
        commits the DRM framebuffer.

        Transition handoff (slice 4-followup): if `next_item` is set
        AND `transition_kind != "cut"` AND `transition_ms > 0`, after
        the slide-duration advance loop ends we call begin_transition
        and keep advancing until the sidecar's state machine promotes
        the to-slide (advance returns PaintSlide(next_item) instead of
        PaintTransition). The next outer iteration's begin_slide on
        next_item re-anchors that slide's clock from 0 -- visible
        state is identical since the sidecar just promoted with
        t_in_slide_ms=0 anyway.

        Cut transitions (transition_kind == "cut" or transition_ms <=
        0) are no-ops here; the outer loop's begin_slide on the next
        iteration is the instant cut.

        TODO: handle `AutoFallbackInMockError` (the wrapper post-
        fallback swap) and `RustRendererRespawnedError` (transient
        subprocess reconnect; caller must replay begin_slide on the
        fresh subprocess). Both currently propagate uncaught and
        would crash the outer `_loop`. Acceptable until the
        respawn-replay path is wired -- the wrapper's swap is
        permanent + an immediate restart-loop will reach a clean
        state.
        """
        from openmarquee.rendering.rust_renderer import (
            PaintTransition,
            RustRendererUnsupportedSlideError,
            RustRendererUnsupportedTransitionError,
            SlideComplete,
        )
        assert self._stop_event is not None
        assert self._pause_event is not None
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        self._slot_t0 = t0
        # Sidecar uses ms-resolution monotonic. t0_ms is the wall-clock
        # anchor passed to begin_slide; advance() ticks are deltas off
        # it. Using int(loop.time() * 1000) keeps the same clock the
        # rest of the loop uses for end_at math.
        t0_ms = int(t0 * 1000)
        duration_ms = int(item.duration_ms)
        try:
            self._renderer.begin_slide(item.id, t0_ms, duration_ms)
        except RustRendererUnsupportedSlideError as e:
            log.info(
                "playback: skipping slide %s (Rust sidecar doesn't yet "
                "support this kind): %s", item.id, e.message,
            )
            return False
        # 30 Hz tick. Matches the auto_tick_seconds-aware cadence the
        # other paths use; the sidecar's internal state machine clamps
        # to its own paint cadence so over-ticking is harmless.
        tick_period = 1.0 / 30
        end_at = t0 + duration_ms / 1000
        while True:
            if self._stop_event.is_set() or self._pause_event.is_set():
                break
            elapsed = loop.time() - t0
            t_ms = t0_ms + int(elapsed * 1000)
            try:
                result = self._renderer.advance(t_ms)
            except RustRendererUnsupportedSlideError as e:
                # Begin_slide accepted the slide but advance hit the
                # unsupported-kind rail (happens for video on the very
                # first paint_slide). Skip gracefully.
                log.info(
                    "playback: slide %s became unsupported mid-play: %s",
                    item.id, e.message,
                )
                return False
            if isinstance(result, SlideComplete):
                # Sidecar's state machine signaled duration-end. Slide
                # finished cleanly; advance to next item.
                break
            remaining = end_at - loop.time()
            if remaining <= 0:
                break
            await self._wait(min(tick_period, remaining))

        # Transition handoff. Only fires when caller passed a non-cut
        # transition AND there's a next slide to transition INTO. Pause
        # or stop events skip transitions -- a paused/stopped loop
        # should yield immediately, not paint another transition_ms.
        if (
            next_item is not None
            and transition_kind != "cut"
            and transition_ms > 0
            and not self._stop_event.is_set()
            and not self._pause_event.is_set()
        ):
            transition_t0_ms = t0_ms + int((loop.time() - t0) * 1000)
            try:
                self._renderer.begin_transition(
                    next_item.id,
                    int(next_item.duration_ms),
                    transition_kind,
                    int(transition_ms),
                    transition_t0_ms,
                )
            except RustRendererUnsupportedTransitionError as e:
                # Forward-compat catch -- today's Rust silently FS_CUT-
                # fallbacks for unknown kinds so this path doesn't fire.
                # If a future Rust change starts erroring explicitly,
                # we log + return cleanly so the next outer iteration
                # begin_slide's the next item (= instant cut, same
                # visible result the silent fallback produced).
                log.info(
                    "playback: transition kind %r unsupported; falling "
                    "through to instant cut: %s", transition_kind, e.message,
                )
                return True
            # Drive the transition window. Exit on:
            #   (a) advance returns PaintSlide(next_item) -- state machine
            #       promoted the to-slide; visible result is identical
            #       to "transition complete, next slide playing"
            #   (b) transition_ms elapsed (safety bound; the sidecar's
            #       clamp at line 159 of playback.rs already guarantees
            #       this but extra round-trip protection costs nothing)
            #
            # Note: a stop or pause inside this window leaves the
            # sidecar's `pending` transition state set until the next
            # begin_slide or close op clears it. Harmless: not a leak,
            # and the next outer iteration's begin_slide re-anchors
            # cleanly. Visible effect of a pause mid-window is a
            # partially-blended frame frozen on screen rather than a
            # clean slide; tracked as a known polish item.
            transition_end_at = loop.time() + transition_ms / 1000
            while True:
                if self._stop_event.is_set() or self._pause_event.is_set():
                    break
                t_ms = t0_ms + int((loop.time() - t0) * 1000)
                try:
                    result = self._renderer.advance(t_ms)
                except RustRendererUnsupportedTransitionError as e:
                    # Mid-transition error -- shouldn't happen (begin_
                    # transition already succeeded), but if it does
                    # treat as cut: log + bail.
                    log.info(
                        "playback: transition %r failed mid-window; "
                        "treating as cut: %s", transition_kind, e.message,
                    )
                    break
                if not isinstance(result, PaintTransition):
                    # Sidecar promoted out of the transition state into
                    # either PaintSlide(next_item) or SlideComplete. The
                    # next outer iteration's begin_slide will pick up
                    # cleanly. Either way, transition is done.
                    break
                remaining = transition_end_at - loop.time()
                if remaining <= 0:
                    break
                await self._wait(min(tick_period, remaining))
        return True

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
        # Stamp for capture_current_frame() so its compose at "now"
        # shares the same elapsed_s clock as the live tick.
        self._slot_t0 = t0
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
        # Freeze-resume from prior shader transition (#217 v2). If
        # the outgoing transition stashed an incoming-side freeze
        # elapsed, the compositor.attach() will stage motion at that
        # elapsed (frozen from shader-anim's last frame), and we set
        # t0 AFTER attach so the first while-loop tick computes
        # elapsed = freeze_elapsed + tiny -- ticker resumes from the
        # frozen position with no jump. The compositor.attach gap
        # itself (~150-300ms) is FROZEN: clock pauses, no advance.
        # If no stash (fresh slide / non-shader path), fall through
        # to fresh t0 = loop.time().
        freeze_elapsed = self._incoming_slide_freeze_elapsed
        self._incoming_slide_freeze_elapsed = None
        # t0 placeholder for end_at; updated after attach if freeze.
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
            snapshot_cache=self._snapshot_cache,
        )
        # If we're inheriting a freeze elapsed from the just-ended
        # shader transition (#217 v2), pass it to attach(). Compositor
        # will stage motion at that exact elapsed (FROZEN clock; the
        # ticker stays where shader-anim left it). After attach, we
        # set t0 such that elapsed-now = freeze_elapsed -- subsequent
        # while-loop ticks then advance smoothly from that position.
        # The 150-300ms attach gap is invisible motion-wise: clock
        # paused; no jump on un-freeze.
        compositor.attach(
            now=datetime.now(tz), freeze_at_elapsed=freeze_elapsed,
        )
        if freeze_elapsed is not None:
            # Reset t0 + end_at so subsequent ticks resume from the
            # frozen position (NOT from real-time-since-transition).
            t0 = loop.time() - freeze_elapsed
            end_at = t0 + total
        # Stamp for capture_current_frame() so its compose shares the
        # same elapsed_s clock as the live tick (post-freeze adjusted).
        self._slot_t0 = t0
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

            # End-of-slide handoff. Two cases:
            #
            # (a) Shader transitions enabled: the next transition reads
            #     bg+statics from snapshot cache, so the dispatcher's
            #     "current_image" return value isn't visually scanned
            #     out. Painting compose_motion_frame here would write a
            #     WRAP-visual ticker (np.roll) to the primary plane,
            #     visible for one vblank between the last steady-state
            #     tick (TRANSLATE position) and the shader's first
            #     frame (TRANSLATE position) -- a TRANSLATE -> WRAP ->
            #     TRANSLATE jump (#217). Skip that paint; return the
            #     cached bg+statics as a PIL Image so the dispatcher
            #     contract holds. The visible primary plane stays at
            #     whatever the steady-state tick last committed until
            #     the shader takes it over.
            #
            # (b) Shader transitions disabled (PIL-fallback path): the
            #     transition's frame loop reads `current_image` and
            #     paints it directly. We need a motion-accurate final
            #     frame here so the transition starts from the right
            #     pixels. compose_motion_frame is correct for that
            #     path; the WRAP/TRANSLATE divergence doesn't matter
            #     because the entire transition runs through the same
            #     PIL math.
            elapsed = loop.time() - t0
            if self._shader_transitions_enabled():
                try:
                    rgba = self._snapshot_cache.get_bg_statics(
                        item,
                        self._renderer.width, self._renderer.height,
                        read_asset=self._read_asset,
                    )
                    return Image.frombytes(
                        "RGBA",
                        (self._renderer.width, self._renderer.height),
                        rgba,
                    ).convert("RGB")
                except Exception:
                    log.exception(
                        "playback: bg+statics fetch failed for shader "
                        "handoff -- returning None will skip transition",
                    )
                    return None
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
            # is responsible for eventual detach: _run_shader_transition
            # detaches at transition end on success, or via
            # _drain_outgoing_compositor on the shader-unavailable
            # fall-through (so the PIL fallback paints onto a clean
            # overlay state). Otherwise (shader path off): detach
            # immediately, no #206 work.
            if self._shader_transitions_enabled():
                self._outgoing_compositor = compositor
                self._outgoing_slide = item  # type: ignore[assignment]
                # Stash the slide's last-tick elapsed so the next
                # transition can FREEZE the motion clock at this value
                # during the upload+drain gap (#218). Without freeze,
                # shader-anim's first frame would compute elapsed via
                # real time and ticker would snap forward by the
                # gap_ms * velocity at the seam.
                self._outgoing_slide_last_elapsed = elapsed
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
        currently active. Test-only setter is just self._current_playlist_id.

        Bug 8 / Fix B (2026-05-17): on playlist change, clear the per-
        slide IPC-failure throttle set so an operator who fixed a
        broken slide (e.g. by switching to a healthier playlist OR by
        re-uploading a single-trak asset) gets ERROR-level visibility
        on the next attempt, not DEBUG-suppressed silence.
        """
        if playlist_id != self._current_playlist_id and self._failed_slide_ids:
            log.info(
                "playback: playlist changed (%s → %s); clearing %d failed-"
                "slide throttle entries",
                self._current_playlist_id, playlist_id, len(self._failed_slide_ids),
            )
            self._failed_slide_ids.clear()
        self._current_playlist_id = playlist_id

    # --- /api/playback/current-frame capture (added 2026-05-06) ----------

    # Cache TTL: bound the GPU-readback / compose-recompose cost. 5 min
    # is the spec-set ceiling; the cache also invalidates immediately
    # when current_playlist_id changes. See `cached_current_frame_png`
    # docstring for the freshness contract.
    _FRAME_CACHE_TTL_S: float = 300.0

    async def cached_current_frame_png(self) -> bytes | None:
        """PNG of what's currently rendering, with a 5-min TTL +
        playlist-change invalidation.

        Cache hit when BOTH:
          - age < 5 minutes since last capture, AND
          - current_playlist_id matches the captured slot

        Otherwise: capture fresh (compose_motion_frame at the live
        elapsed_s, or the asset PNG for image slides), store, return.
        Concurrent callers serialize behind a lock so a burst of
        requests issues exactly one capture; subsequent waiters get
        the freshly cached frame.

        Returns None when nothing is currently playing or capture
        otherwise fails (the API layer maps None to 503). For image
        slides the asset PNG IS the current frame -- no recompose
        needed. For text slides we re-run compose_motion_frame at the
        live elapsed_s so motion + auto-mode (clock, etc.) reflect
        the actual on-screen state, not a stale rasterize. Video
        slides return None today (writeback from the hardware decoder
        path isn't wired up; not a regression because nothing was
        capturing video frames before).
        """
        loop = asyncio.get_event_loop()
        now_mono = loop.time()
        cached = self._frame_cache
        current_playlist = self._current_playlist_id
        if (
            cached is not None
            and (now_mono - cached[1]) < self._FRAME_CACHE_TTL_S
            and cached[2] == current_playlist
        ):
            return cached[0]
        # Need fresh capture. Lock-bind on first use; the loop's start
        # rebinds events anyway, but the lock survives across runs
        # since it's only contended when capture is mid-flight.
        if self._frame_capture_lock is None:
            self._frame_capture_lock = asyncio.Lock()
        async with self._frame_capture_lock:
            # Re-check inside the lock -- another waiter may have
            # refreshed the cache while we were queued.
            cached = self._frame_cache
            now_mono = loop.time()
            if (
                cached is not None
                and (now_mono - cached[1]) < self._FRAME_CACHE_TTL_S
                and cached[2] == current_playlist
            ):
                return cached[0]
            slot_t0 = self._slot_t0
            elapsed_s = (
                0.0 if slot_t0 is None else max(0.0, loop.time() - slot_t0)
            )
            try:
                png = await asyncio.to_thread(
                    self._capture_current_frame_sync, elapsed_s
                )
            except Exception:
                log.exception("playback: capture_current_frame failed")
                # Fall back to whatever the previous cache had so a
                # transient failure (e.g. mid-transition race, brief
                # font-load stall) doesn't 503 the whole endpoint.
                return cached[0] if cached else None
            if png is None:
                return cached[0] if cached else None
            self._frame_cache = (png, loop.time(), current_playlist)
            return png

    def _capture_current_frame_sync(self, elapsed_s: float) -> bytes | None:
        """Synchronous worker for cached_current_frame_png. Runs on a
        thread -- compose_motion_frame is CPU-bound PIL work and
        shouldn't block the event loop. Caller passes elapsed_s
        because asyncio's loop.time() can't be read from a worker
        thread."""
        item_id = self._current_id
        if item_id is None:
            return None
        try:
            items = self._fetch_items()
        except Exception:
            log.exception("playback: capture fetch_items failed")
            return None
        item = next((it for it in items if it.id == item_id), None)
        if item is None:
            return None
        if item.type == "text_slide":
            tz = resolve_timezone(self._get_timezone())
            try:
                background_cache = load_motion_background(
                    item, self._renderer.width, self._renderer.height,
                    self._read_asset,
                )
            except Exception:
                background_cache = None
            try:
                layer_bitmap_cache = prerender_layer_bitmaps(
                    item, self._renderer.width, self._renderer.height,
                )
            except Exception:
                layer_bitmap_cache = None
            img = compose_motion_frame(
                item,
                elapsed_s,
                self._renderer.width,
                self._renderer.height,
                read_asset=self._read_asset,
                now=datetime.now(tz),
                background_cache=background_cache,
                layer_bitmap_cache=layer_bitmap_cache,
            )
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        if item.type == "image":
            # The asset PNG already IS the rendered frame -- images
            # don't have motion / auto-mode -- so just relay it.
            try:
                return self._read_asset(item.id)
            except Exception:
                log.exception("playback: capture image asset read failed")
                return None
        # Video and any future type: no readback path yet.
        return None

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

        Animated text on either side is composited in-shader via
        per-side anim texture units (#215): the outgoing slide's first
        animated layer (and the incoming slide's first) are rasterized
        once at transition start, uploaded, and updated per frame with
        motion math (ticker / breathe / pulse / bounce / blink). The
        transition shader's per-pixel mask then clips the composited
        anim+base correctly -- iris, wipe, dissolve etc. all separate
        the two slides cleanly even with motion. Pre-#215 this was
        attempted via HVS overlay planes layered ON TOP of the shader
        primary, but HVS can only do plane-uniform alpha (no per-pixel
        shape clip), so motion bled through the iris boundary. Pulling
        motion INTO the shader's primary plane is the fix.

        Shape:
          1. Compose u_from / u_to bg+statics-only RGBA snapshots.
          2. Extract one animated layer per side (if present),
             rasterize glyph bbox, set_X_anim().
          3. Detach outgoing compositor (its overlay slots are about
             to be re-claimed by the next slide's GPUSlideCompositor;
             motion now lives in the shader, not on overlays).
          4. set_kind(kind), set_from(...), set_to(...).
          5. Frame loop: per-frame motion math -> update_X_anim;
             set_transition_t(t in [0,1]) + commit_frame +
             cooperative-pause-aware sleep.
          6. Hand the primary plane back to multi-plane DRMRenderer in
             one atomic commit: render_frame(to_image), restage_primary
             _fb(), commit.
        """
        import time as _time
        _enter_t = _time.monotonic()
        if kind not in _SHADER_TRANSITION_KINDS:
            # Shouldn't be reached today (every kind is in the set);
            # kept for forward-compat if a future kind is wired into
            # a dispatcher before its fragment shader exists.
            self._drain_outgoing_compositor()
            return False
        sr = self._get_or_create_shader_renderer()
        if sr is None:
            # Shader unavailable (env flag off, dev host without
            # libdrm/libegl, or construction failed). Drain outgoing
            # so the caller's PIL fallback paints onto a clean
            # overlay-slot state -- otherwise the still-attached
            # outgoing overlays would double-paint with the PIL
            # transition's render_image output.
            self._drain_outgoing_compositor()
            return False
        renderer = self._renderer
        width, height = sr.width, sr.height

        _post_sr_t = _time.monotonic()
        # u_from / u_to: bg+statics-only snapshots of each side. The
        # animated layers are pulled out separately and uploaded to
        # the per-side anim texture units below; this two-channel
        # split is what lets the transition mask clip motion correctly
        # per-pixel.
        outgoing_slide = self._outgoing_slide
        from_rgba: bytes
        if outgoing_slide is not None:
            try:
                # Cached snapshot path (#205): microseconds on the
                # second+ visit, ~600ms first time.
                from_rgba = self._snapshot_cache.get_bg_statics(
                    outgoing_slide, width, height,
                    read_asset=self._read_asset,
                )
            except Exception:
                log.exception(
                    "playback: bg+statics compose failed for outgoing "
                    "slide; falling back to full from_image"
                )
                from_rgba = self._image_to_rgba_bytes(from_image, width, height)
        else:
            from_rgba = self._image_to_rgba_bytes(from_image, width, height)

        to_rgba: bytes
        incoming_slide = self._incoming_slide
        if incoming_slide is not None:
            try:
                to_rgba = self._snapshot_cache.get_bg_statics(
                    incoming_slide, width, height,
                    read_asset=self._read_asset,
                )
            except Exception:
                log.exception(
                    "playback: bg+statics compose failed for incoming "
                    "slide; falling back to full to_image"
                )
                to_rgba = self._image_to_rgba_bytes(to_image, width, height)
        else:
            to_rgba = self._image_to_rgba_bytes(to_image, width, height)

        # Extract first animated layer per side (#215). Cached on
        # SlideSnapshotCache (#216): warm-cache hits in microseconds,
        # cold path pays ~100ms PIL rasterize. Background prerender
        # at slide-attach time fills the cache before the transition
        # fires so even cold-cache transitions start instantly.
        _post_bg_t = _time.monotonic()
        outgoing_anim = (
            self._snapshot_cache.get_anim_layer(
                outgoing_slide, width, height,
            ) if outgoing_slide is not None else None
        )
        incoming_anim = (
            self._snapshot_cache.get_anim_layer(
                incoming_slide, width, height,
            ) if incoming_slide is not None else None
        )
        _post_anim_t = _time.monotonic()
        log.info(
            "playback: transition setup timing -- sr=%.0fms, bg=%.0fms, "
            "anim=%.0fms (anim cache: out=%s, in=%s)",
            (_post_sr_t - _enter_t) * 1000,
            (_post_bg_t - _post_sr_t) * 1000,
            (_post_anim_t - _post_bg_t) * 1000,
            "hit" if outgoing_anim is not None else "miss/none",
            "hit" if incoming_anim is not None else "miss/none",
        )

        # Capture outgoing motion-clock anchor BEFORE drain (#217 v2).
        # _drain_outgoing_compositor clears self._outgoing_slide_*
        # state. The OUTGOING ANCHOR is (anchor_elapsed, anchor_time):
        # shader-anim's elapsed = anchor_elapsed + (now - anchor_time).
        # anchor_elapsed = the elapsed value the slide had at its
        # last steady tick (so shader-anim's ticker resumes from the
        # SAME position the user just saw on the overlay plane).
        # anchor_time will be set right at frame loop start so the
        # texture upload + drain gap doesn't count as motion -- the
        # clock pauses during the freeze, no jump on screen un-freeze.
        outgoing_anchor_elapsed = (
            self._outgoing_slide_last_elapsed
            if self._outgoing_slide_last_elapsed is not None
            else 0.0
        )

        # Track why we exit the frame loop. Pause means a stream
        # takeover is becoming the new owner of render_frame/commit
        # (SYSTEM_SPEC §5.11) -- racing it with our handoff would
        # double-commit the primary plane. Skip the handoff in that
        # case and let the takeover own the plane. On stop or normal
        # completion the handoff is correct and required.
        paused = False
        try:
            # Texture uploads BEFORE drain (#217). 1080p RGBA u_from +
            # u_to are ~8MB each and the upload to GPU on Pi Zero 2 W
            # is ~30-80ms per texture. If we drain first then upload,
            # the outgoing slide's overlay text vanishes for ~70-170ms
            # (no overlay, primary unchanged at bg+statics, ticker
            # invisible). Reordering keeps overlays alive (frozen at
            # T_last position) during the upload window so the only
            # "no ticker" gap is between drain and the first shader
            # commit -- ~10ms, imperceptible.
            sr.set_kind(kind)
            sr.set_from(from_rgba, width, height)
            sr.set_to(to_rgba, width, height)
            sr.clear_anim()
            if outgoing_anim is not None:
                _, _, rgba, gbbox, _ = outgoing_anim
                sr.set_from_anim(rgba, gbbox[2], gbbox[3])
            if incoming_anim is not None:
                _, _, rgba, gbbox, _ = incoming_anim
                sr.set_to_anim(rgba, gbbox[2], gbbox[3])
            _post_upload_t = _time.monotonic()

            # Drain outgoing overlays now that shader is loaded and
            # the next commit_frame is microseconds away. Without
            # shader-side anim (#215) the overlay-plane scanout was
            # the canonical motion source; now overlays are obsolete
            # the moment shader takes the primary plane.
            self._drain_outgoing_compositor()
            _post_drain_t = _time.monotonic()

            n_frames = max(1, int(transition_ms / 1000 * _FADE_FPS))
            frame_period = (transition_ms / 1000) / n_frames
            assert self._stop_event is not None
            assert self._pause_event is not None
            # Anchor times for both motion clocks (#217 v2). The
            # texture-upload + drain gap that just ran is FROZEN --
            # not counted toward motion. anchor_time = NOW; anchors
            # advance from here as wall-clock time progresses inside
            # the frame loop.
            transition_start = _time.monotonic()
            outgoing_anchor_time = transition_start
            incoming_anchor_time = transition_start
            incoming_anchor_elapsed = 0.0  # incoming enters fresh
            log.info(
                "playback: transition phase timing -- upload=%.0fms "
                "drain=%.0fms (now entering frame loop, %d frames @ %.0fms)",
                (_post_upload_t - _post_anim_t) * 1000,
                (_post_drain_t - _post_upload_t) * 1000,
                n_frames, frame_period * 1000,
            )
            frames_drawn = 0
            has_motion = (
                outgoing_anim is not None or incoming_anim is not None
            )
            # Track incoming's last elapsed so we can stash it on
            # success for the next compositor.attach to inherit
            # (freeze-resume: ticker resumes from this exact position
            # without a jump even though attach takes ~150-300ms).
            last_elapsed_in = incoming_anchor_elapsed
            for i in range(1, n_frames + 1):
                if self._pause_event.is_set():
                    paused = True
                    break
                if self._stop_event.is_set():
                    break
                t = i / n_frames
                sr.set_transition_t(t)
                # Anchor-based clock: elapsed = anchor_elapsed +
                # (now - anchor_time). Outgoing resumes from its
                # last-steady-tick elapsed; incoming starts at 0
                # and advances. The pre-frame-loop gap (texture
                # upload + drain) is FROZEN -- doesn't count.
                _now = _time.monotonic()
                if outgoing_anim is not None:
                    _, layer, _, gbbox, bx = outgoing_anim
                    elapsed_out = (
                        outgoing_anchor_elapsed
                        + (_now - outgoing_anchor_time)
                    )
                    box_uv, alpha = self._anim_uv_for_frame(
                        layer, bx, gbbox, width, height, elapsed_out,
                    )
                    sr.update_from_anim(box_uv, alpha)
                if incoming_anim is not None:
                    _, layer, _, gbbox, bx = incoming_anim
                    elapsed_in = (
                        incoming_anchor_elapsed
                        + (_now - incoming_anchor_time)
                    )
                    last_elapsed_in = elapsed_in
                    box_uv, alpha = self._anim_uv_for_frame(
                        layer, bx, gbbox, width, height, elapsed_in,
                    )
                    sr.update_to_anim(box_uv, alpha)
                sr.commit_frame()
                frames_drawn += 1
                # Skip the wait after the LAST frame -- it's pure
                # dead air before the next slide's compositor.attach,
                # adding ~one frame_period (~33ms at 30fps) to the
                # seam freeze for no benefit. Pacing matters between
                # frames; after the last frame the loop's done (#218).
                if i < n_frames:
                    await self._wait(frame_period)
            transition_elapsed = _time.monotonic() - transition_start
            achieved_fps = (
                frames_drawn / transition_elapsed if transition_elapsed > 0 else 0.0
            )
            log.info(
                "playback: shader transition %r %s: %d/%d frames in "
                "%.2fs (%.1f fps achieved, target %.1f)",
                kind,
                "with-motion" if has_motion else "snapshot-only",
                frames_drawn, n_frames,
                transition_elapsed, achieved_fps, _FADE_FPS,
            )
            if not paused and not self._stop_event.is_set():
                # The frame loop's last iteration (i=n_frames) already
                # fires at t = n_frames/n_frames = 1.0, so the prior
                # post-loop "land at t=1.0" frame was redundant -- the
                # shader's final commit already had t=1.0 content.
                # Removed (#218): saves ~50ms per transition (one full
                # commit_frame + atomic ioctl) of dead work that
                # showed visually identical pixels.
                #
                # Stash for #217 v2: the incoming slide's compositor
                # will FREEZE-RESUME at this elapsed when it attaches.
                # The compositor.attach gap is invisible motion-wise
                # -- ticker stays at last_elapsed_in's position
                # through the freeze, then resumes naturally from
                # there. Set ONLY on success: a paused / stopped /
                # exception transition shouldn't leak stale state
                # into the next slide's compositor.
                self._incoming_slide_freeze_elapsed = last_elapsed_in
        except Exception:
            log.exception(
                "playback: shader transition %r failed mid-flight; "
                "primary plane will be reset to multi-plane content",
                kind,
            )
            # Fall through to the handoff dance anyway (unless paused)
            # so the screen recovers cleanly after a shader-side error.

        if paused:
            # Stream takeover owns the plane now. Don't fight it.
            return True

        # For TextSlide-incoming: don't restage primary here. The
        # incoming compositor.attach() will paint slide B's bg+statics
        # into the multi-plane dumb buffer AND stage primary FB_ID +
        # overlays + motion-derived crtc_x in a single atomic commit.
        # Until that commit fires, the shader's last fb stays on
        # primary -- continuous motion at the seam (#217).
        #
        # For non-TextSlide incoming (image/video): no compositor
        # attaches. _safe_load_image + _render_image paints the multi-
        # plane dumb buffer but doesn't stage FB_ID, so without a
        # restage here the kernel keeps showing shader's last fb for
        # the entire next slide's duration -- the image/video would
        # be invisible. Paint to_image (full composite, RGB) and
        # restage primary FB_ID so the image actually appears.
        if incoming_slide is None:
            try:
                handoff_rgb = (
                    Image.frombytes("RGBA", (width, height), to_rgba)
                    .convert("RGB")
                    .tobytes()
                )
                renderer.render_frame(handoff_rgb)
                renderer.restage_primary_fb()
                renderer.commit()
            except Exception:
                log.exception(
                    "playback: post-shader-transition handoff to "
                    "multi-plane failed for non-text-slide incoming; "
                    "screen may stay on shader's last frame",
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

    def _anim_uv_for_frame(
        self,
        layer: "TextLayer",
        box_px: tuple[int, int, int, int],
        glyph_bbox_px: tuple[int, int, int, int],
        sign_w: int,
        sign_h: int,
        elapsed_s: float,
    ) -> tuple[tuple[float, float, float, float], float]:
        """Compute per-frame (box_uv, alpha) for `layer` at `elapsed_s`
        on the slide's tick clock. Mirrors GPUSlideCompositor._stage_
        motion's per-effect math but emits SCREEN-UV box rect + 0..1
        alpha instead of CRTC pixel coords + 16-bit plane alpha. The
        two paths share `compute_phase` + `_effect_freq` so phase math
        on slide A's overlay-plane motion (pre-transition) is
        identical to slide A's shader-anim motion (during transition)
        -- continuous animation across the seam.

        Returns ((x, y, w, h), alpha) where x/y/w/h ∈ [0, 1] in
        screen-UV (top-down origin) and alpha ∈ [0, 1]."""
        motion = getattr(layer, "motion", "static") or "static"
        intensity = int(getattr(layer, "motion_intensity", 50))
        motion_phase_offset = float(getattr(layer, "motion_phase", 0.0))
        # B2 (2026-05-05): motion_speed multiplies the effect's frequency.
        # Mirrors the same scaling on the software + multi-plane paths so
        # all three render surfaces tick at the operator-set speed.
        speed_raw = getattr(layer, "motion_speed", 1.0)
        speed = max(0.0, min(2.0, float(speed_raw if speed_raw is not None else 1.0)))
        phase = compute_phase(
            elapsed_s, _effect_freq(motion, intensity) * speed, motion_phase_offset,
        )
        bx, by, bw, bh = box_px
        gx, gy, gw, gh = glyph_bbox_px
        # Defaults: glyph at rest position, fully opaque.
        crtc_x, crtc_y, crtc_w, crtc_h = gx, gy, gw, gh
        alpha = 1.0
        if motion == "ticker":
            # Sweep glyph leftward across the box. Mirrors
            # _stage_motion's ticker. Same snap-then-restart behavior.
            sweep_total = bw + gw
            crtc_x = bx + bw - int(round(phase * sweep_total))
        elif motion == "breathe":
            amplitude = (intensity / 100.0) * 0.20
            s = 1.0 + amplitude * math.sin(2 * math.pi * phase)
            new_w = max(1, int(round(gw * s)))
            new_h = max(1, int(round(gh * s)))
            box_cx = bx + bw / 2.0
            box_cy = by + bh / 2.0
            glyph_cx = gx + gw / 2.0
            glyph_cy = gy + gh / 2.0
            new_cx = box_cx + s * (glyph_cx - box_cx)
            new_cy = box_cy + s * (glyph_cy - box_cy)
            crtc_x = int(round(new_cx - new_w / 2.0))
            crtc_y = int(round(new_cy - new_h / 2.0))
            crtc_w = new_w
            crtc_h = new_h
        elif motion == "pulse":
            min_a = 1.0 - intensity / 100.0
            s = (math.sin(2 * math.pi * phase) + 1.0) / 2.0
            alpha = min_a + (1.0 - min_a) * s
        elif motion == "bounce":
            amp = (intensity / 100.0) * 0.10
            offset_px = -int(round(amp * bh * abs(math.sin(2 * math.pi * phase))))
            crtc_y = gy + offset_px
        elif motion == "blink":
            # Square wave: half cycle on, half off. Matches blink's
            # alpha pattern in _stage_motion.
            alpha = 1.0 if phase < 0.5 else 0.0
        # Other kinds (shake / unknown): freeze at rest position --
        # acceptable since transition windows are short (200-1000ms)
        # and shake's deterministic-Gaussian step counter would need
        # to thread layer.id through here.
        return (
            (
                crtc_x / sign_w, crtc_y / sign_h,
                crtc_w / sign_w, crtc_h / sign_h,
            ),
            max(0.0, min(1.0, alpha)),
        )

    async def _fade(
        self,
        from_image: Image.Image,
        to_image: Image.Image,
        transition_ms: int,
    ) -> None:
        """Alpha-blend from `from_image` to `to_image` over `transition_ms`.

        Routes through the shader compositor when available -- same
        pattern every other transition kind uses since the unification
        cleanup. The shader's mix(u_from, u_to, t) is mathematically
        identical to the prior _fade_gpu's HVS plane.alpha animation
        but goes through the same orchestration so motion-through-
        transitions (#206) works for fade as well. PIL Image.blend
        is the fallback for non-DRM renderers (MockRenderer, LED
        matrices, fb0).

        Returns early on stop OR pause request -- pause-awareness keeps
        the transition from painting playlist frames over an in-flight
        stream takeover.
        """
        if await self._run_shader_transition(
            from_image, to_image, "fade", transition_ms,
        ):
            return
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

        Routes through the shader compositor when available -- same
        pattern every other transition kind uses since the unification
        cleanup. The shader's per-fragment x-coordinate threshold is
        equivalent to the prior _wipe_gpu's HVS CRTC_W animation but
        goes through the same orchestration so motion-through-
        transitions (#206) works for wipe as well. PIL paste-and-crop
        is the fallback for non-DRM renderers (MockRenderer, LED
        matrices, fb0).

        Returns early on stop. Same frame cadence as _fade so the two
        transitions feel like the same smoothness at equal transition_ms.
        """
        if await self._run_shader_transition(
            from_image, to_image, "wipe", transition_ms,
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
