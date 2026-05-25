"""Background playback loop that drives a renderer via IPC ops.

The loop runs as an asyncio task. On each iteration it:

- fetches the current list of items (via an injected callable)
- if empty, sleeps briefly and rechecks
- otherwise drives each item through the renderer's IPC contract
  (begin_slide / advance / begin_transition / capture) until the
  sidecar's state machine reports SlideComplete

Post-DELETE-PIL (slices 1-12, 2026-05-17): there is no Python
rasterization path. The Rust IPC sidecar (`RustRenderer`,
`AutoFallbackRenderer`) owns rendering on production; `MockRenderer`
satisfies the same IPC ops contract for dev/CI. The loop never
composes pixels itself; it sends ops and waits for completion.

Items are re-fetched between iterations, so adding/deleting slides
while playing takes effect on the next cycle without restarting the
loop. A failed render is logged and skipped — one bad slide doesn't
kill the loop (per-slide error throttle prevents log spam from a
permanently broken slide).
"""

import asyncio
import contextlib
import logging
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from openmarquee.content import ContentItem, StreamSlide, WebSlide
from openmarquee.rendering import Renderer
from openmarquee.stream_consumer import StreamConsumer

if TYPE_CHECKING:
    from openmarquee.content.storage import ContentStorage
    from openmarquee.playlist import PlaylistStorage
    from openmarquee.schedule import ScheduleStorage

log = logging.getLogger(__name__)

# STREAM/VLC Mode B (docs/STREAM_VLC_PROPOSAL.md §6 / Q13): how long to
# wait for the first frame from a StreamSlide's stream source before
# treating the URL as unreachable and applying the slide's
# on_unreachable policy. Short enough that a dead URL doesn't dominate
# a 10-second slot.
_STREAM_CONNECT_TIMEOUT_S = 3.0


def web_refresh_due(
    last_fetch_monotonic: float | None,
    now_monotonic: float,
    refresh_interval_s: float,
) -> bool:
    """Pure staleness predicate for a Web slide's screenshot refresh.

    A Web slide's `asset.png` is a screenshot re-rendered on-device
    every `refresh_interval_s`. When the slide's slot comes round,
    the playback loop asks this whether a fresh render is owed.

    Args:
        last_fetch_monotonic: when a refresh was last KICKED for this
            slide (monotonic clock), or None if one never has been.
        now_monotonic: the current monotonic time.
        refresh_interval_s: the slide's `refresh_interval_s`.

    Returns:
        True when a refresh is due: always True the first time (no
        prior fetch), then True once `refresh_interval_s` has elapsed
        since the last kick. The timestamp is stamped at KICK time
        (not completion) so a slow in-flight fetch doesn't make the
        loop re-kick it on the next slot — the in-flight set handles
        the same-slide double-kick guard, and stamping-at-kick keeps
        the cadence steady regardless of fetch latency.
    """
    if last_fetch_monotonic is None:
        return True
    return (now_monotonic - last_fetch_monotonic) >= refresh_interval_s


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
        stuck_backoff_seconds: float = 3.0,
        active_playlist_id: Callable[[], UUID | None] | None = None,
        web_screenshot_producer: (Callable[[WebSlide, int, int], Awaitable[bool]] | None) = None,
    ):
        self._renderer = renderer
        self._fetch_items = fetch_items
        self._read_asset = read_asset
        # Web slide: the screenshot-refresh producer. A coroutine
        # that, given a WebSlide + the display width/height, renders a
        # fresh screenshot on-device and saves it as the slide's
        # asset.png. The loop fire-and-forgets it (asyncio.create_task)
        # when a Web slide's slot is stale — it is NEVER awaited inside
        # _loop, so a multi-second render can't freeze the sign. None
        # in tests / configs that don't wire a producer (a Web slide
        # then just renders its current asset).
        self._web_screenshot_producer = web_screenshot_producer
        # Bug 1 (2026-05-20): a cheap "which playlist is active right
        # now per the schedule" probe, re-evaluated once per slot so a
        # schedule/playlist switch preempts the running playlist
        # instead of waiting for it to finish its loop. None in tests
        # that don't exercise mid-loop preemption.
        self._active_playlist_id_fn = active_playlist_id
        self._empty_poll = empty_playlist_poll_seconds
        # Bug 8 gap (2026-05-20): backoff floor when a full playlist
        # pass yields ZERO playable slides (every slide skipped as an
        # unsupported kind, or failed IPC). Without it the outer loop
        # re-fetches + re-iterates at render-thread speed and starves
        # the sign (qarl's one-bad-video "coffe" playlist froze FYS).
        # Tests override to a small value.
        self._stuck_backoff = stuck_backoff_seconds
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
        # Pause/resume for live takeover (SYSTEM_SPEC §5.11). When a
        # live session is active, it pause()s the loop, takes over
        # render_frame() calls itself, then resume()s when done. Two
        # events because asyncio.Event has no "wait for clear": _pause
        # set means "stop rendering and yield", _resume set means
        # "start rendering again". They're mutually exclusive — pause()
        # and resume() flip them as a pair.
        self._pause_event: asyncio.Event | None = None
        self._resume_event: asyncio.Event | None = None
        # QA perf-resweep v2 P2 (2026-05-24): single wake signal that
        # _wait races against, set by both stop() and pause(). Replaces
        # the prior 2-Task-per-call pattern (asyncio.create_task on each
        # of _stop_event.wait() + _pause_event.wait(), then
        # asyncio.wait + cleanup) -- per-tick allocation cost ~0.45% of
        # one core during active playback. With wake_event we wrap a
        # single coroutine in asyncio.wait_for (1 Task wrap per call,
        # not 2) and drop the cancel/await cleanup entirely.
        self._wake_event: asyncio.Event | None = None
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
        # Bug 8 gap (2026-05-20): per-slide throttle for the
        # unsupported-KIND skip log. Fix B's _failed_slide_ids only
        # throttles the IPC-FAILURE path; the unsupported-kind
        # pre-flight skip (begin_slide / advance raising
        # UnsupportedSlideError, caught inside _play_via_rust_ipc)
        # was unthrottled and spammed ~50 lines/sec on an all-bad
        # playlist. Same shape as _failed_slide_ids: first skip per
        # id logs INFO, subsequent log DEBUG. Reset on playlist
        # change by _stamp_playlist_id.
        self._skipped_slide_ids: set[UUID] = set()
        # True while the active playlist has zero playable slides —
        # gates the zero-playable warning to one line per stuck
        # episode (then DEBUG), and a recovery INFO when it clears.
        self._all_unplayable: bool = False
        # Web slide (Web slide P3): per-slide-id monotonic timestamp of
        # the last screenshot refresh KICK. web_refresh_due() compares
        # against this to decide staleness; a slide id absent from the
        # dict has never been fetched and is due on first sight.
        self._web_last_fetch: dict[UUID, float] = {}
        # Web slide (P3): slide ids with a screenshot fetch CURRENTLY
        # in flight. Guards against double-kicking the same slide while
        # a previous fetch (2-10s) is still running — without it a Web
        # slide whose slot recurs faster than its fetch finishes would
        # stack tasks. The fetch coroutine's done-callback clears the
        # id (see _kick_web_refresh).
        self._web_inflight: set[UUID] = set()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def is_paused(self) -> bool:
        """True when a pause has been requested and not yet resumed.

        Live takeover (SYSTEM_SPEC §5.11) flips this on while a
        live session is active so the playback loop yields the
        renderer to the session's frame source.
        """
        return self._pause_event is not None and self._pause_event.is_set()

    @property
    def renderer(self) -> Renderer:
        """The renderer the loop drives. Exposed so a paused loop's
        external frame source (e.g. the live session) can push frames
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
        self._wake_event = asyncio.Event()
        # Initial state: not paused, so resume_event is set so any caller
        # that checks "are we allowed to run?" sees true. _pause_event is
        # the inverse signal — set means "yield."
        self._resume_event.set()
        self._resume_at_index = None
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
                "playback: _loop task crashed -- sign will be frozen until restart. Cause: %r",
                exc,
                exc_info=exc,
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
        # Wake any in-flight _wait so the loop coroutine exits promptly
        # rather than draining its current sleep.
        if self._wake_event is not None:
            self._wake_event.set()
        try:
            await task
        finally:
            self._stop_event = None
            self._pause_event = None
            self._resume_event = None
            self._wake_event = None
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

            # C3/M1 (2026-05-20): prune the per-slide Web-refresh
            # tracking dicts so a sign whose playlist churns through
            # many Web slides over months doesn't leak an entry per id
            # forever. Done once per outer-loop pass, after the fetch,
            # so `items` is in hand. Runs even on the empty-items path
            # below (a playlist emptied of Web slides should release
            # their entries) — hence before the `if not items` check.
            self._prune_web_tracking(items)

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
            #
            # Bug 8 gap (2026-05-20): track whether ANY slide played
            # this pass. If a full pass (no stop/pause break) plays
            # nothing — every slide skipped/failed — the `for ... else`
            # below applies the zero-playable backoff floor.
            any_played = False
            for i in range(start_idx, len(items)):
                if self._stop_event.is_set():
                    break

                # Pause-check at the top of each iteration: if a stream
                # takeover requested pause, save where we are so the
                # outer-while resumes here, and yield the renderer.
                if self._pause_event.is_set():
                    self._resume_at_index = i
                    break

                # Bug 1 (2026-05-20): a schedule/playlist switch must
                # preempt the running playlist, not wait for it to
                # finish its loop. Re-evaluate the active playlist each
                # slot; if it changed, break to the outer while, which
                # re-fetches and plays the new playlist from index 0
                # (no _resume_at_index — this is a switch, not a pause).
                if self._active_playlist_id_fn is not None:
                    try:
                        active_now = self._active_playlist_id_fn()
                    except Exception:
                        log.exception("playback: active-playlist re-check failed")
                        active_now = self._current_playlist_id
                    if active_now != self._current_playlist_id:
                        log.info(
                            "playback: active playlist changed mid-loop (%s → %s); preempting",
                            self._current_playlist_id,
                            active_now,
                        )
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

                # Web slide: when a Web slide's slot is entered,
                # fire-and-forget a screenshot refresh if the current
                # asset is stale. _maybe_kick_web_refresh launches the
                # render with create_task and returns IMMEDIATELY — the
                # loop NEVER awaits it, so a multi-second render can't
                # freeze the sign. The slide then
                # plays via the normal IPC route below (a `web` slide
                # is an image slide to the renderer — see content.rs
                # find_image_slide), painting whatever asset.png
                # currently exists; the fresh screenshot shows up on
                # the next slot.
                if isinstance(item, WebSlide):
                    self._maybe_kick_web_refresh(item)

                # STREAM/VLC Mode B: a StreamSlide can't go through
                # the sidecar — the Rust ContentItem enum has no
                # stream envelope kind. Intercept it here, BEFORE
                # _play_via_rust_ipc, and run the direct-render stream
                # pump instead (docs/STREAM_VLC_PROPOSAL.md §6). The
                # slot bookkeeping above (_current_*/_slot_t0) is
                # already stamped, so /api/playback/state still reports
                # this slot correctly.
                if item.type == "stream":
                    try:
                        played = await self._play_stream_slide(item)
                    except Exception as e:
                        # Same per-slide failure throttle as the IPC
                        # path: first fail per id carries the
                        # traceback, repeats drop to DEBUG.
                        if item.id in self._failed_slide_ids:
                            log.debug(
                                "playback: stream playback failed for slide id=%s (throttled): %s",
                                item.id,
                                e,
                            )
                        else:
                            self._failed_slide_ids.add(item.id)
                            log.exception(
                                "playback: stream playback failed "
                                "for slide id=%s; advancing to next "
                                "item",
                                item.id,
                            )
                        await self._wait(0.25)
                        continue
                    if self._stop_event.is_set():
                        break
                    if self._pause_event.is_set():
                        self._resume_at_index = i
                        break
                    if played:
                        any_played = True
                    continue

                # IPC route. After the DELETE-PIL purge (slices 1-12)
                # there is no PIL fallback: every renderer the loop is
                # given satisfies the begin_slide / advance / begin_
                # transition contract (MockRenderer became IPC-shaped
                # in slice 11). The sidecar owns rasterization + DRM
                # page-flip; we send ops and wait for SlideComplete +
                # the transition state machine to promote into the
                # to-slide.
                next_for_transition = items[(i + 1) % len(items)] if len(items) > 1 else None
                # 2026-05-17 frozen-sign guard: catch broadly so a
                # single-slide IPC fault doesn't take the whole sign
                # down; log full traceback so the journal carries
                # the diagnosis.
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
                    # fails for the SAME id log DEBUG only.
                    if item.id in self._failed_slide_ids:
                        log.debug(
                            "playback: IPC playback failed for "
                            "slide id=%s (throttled; first fail "
                            "carried the traceback): %s",
                            item.id,
                            e,
                        )
                    else:
                        self._failed_slide_ids.add(item.id)
                        log.exception(
                            "playback: IPC playback failed for slide "
                            "id=%s type=%s; advancing to next item "
                            "(subsequent fails for this id will be "
                            "throttled to DEBUG)",
                            item.id,
                            item.type,
                        )
                    # Brief settle so a hot-loop of failing slides
                    # doesn't burn CPU.
                    await self._wait(0.25)
                    continue
                if self._stop_event.is_set():
                    break
                if self._pause_event.is_set():
                    self._resume_at_index = i
                    break
                # `rendered` True = the slide played out; False =
                # UnsupportedSlideError fired and was logged by
                # _play_via_rust_ipc (sidecar healthy, slide skipped).
                if rendered:
                    any_played = True
            else:
                # Zero-playable-slides floor (Bug 8 gap, 2026-05-20).
                # `for ... else`: this runs only when the loop ran a
                # FULL pass with no break — i.e. NOT a stop/pause exit.
                # If nothing played, every slide was skipped (an
                # unsupported kind) or failed (IPC). Without a floor
                # the outer `while` re-fetches + re-iterates at render-
                # thread speed, starving the render thread and freezing
                # the sign — qarl's one-bad-video "coffe" playlist
                # incident. Back off so we re-fetch at most once per
                # `_stuck_backoff` while stuck. The per-iteration
                # re-fetch still happens (just rate-limited), so a
                # schedule edit that brings playable content in
                # recovers on the next pass. The sidecar holds its
                # last framebuffer while we back off — the last good
                # frame stays on glass, not a frozen-hard sign.
                if items and not any_played:
                    if not self._all_unplayable:
                        self._all_unplayable = True
                        log.warning(
                            "playback: active playlist has no playable "
                            "slides (%d item(s), all skipped/failed) — "
                            "holding last frame, re-checking every %.1fs",
                            len(items),
                            self._stuck_backoff,
                        )
                    else:
                        log.debug(
                            "playback: still no playable slides (%d item(s)); backing off %.1fs",
                            len(items),
                            self._stuck_backoff,
                        )
                    await self._wait(self._stuck_backoff)
                elif any_played and self._all_unplayable:
                    self._all_unplayable = False
                    log.info(
                        "playback: playable content returned — resuming normal cadence",
                    )

    async def _wait(self, seconds: float) -> None:
        """Sleep up to `seconds`, returning early on stop or pause request.

        Pause-awareness keeps live takeover responsive: without it, a
        5-second slide on screen would mean up to 5s of playlist render
        before the live session's pause() actually yields the renderer.

        QA perf-resweep v2 P2 (2026-05-24): single wake_event race instead
        of the prior 2-Task asyncio.wait pattern. Single-threaded asyncio
        means clear()-then-is_set() runs atomically (no await between);
        a set() that lands during the wait_for's await resolves it
        immediately because wake_event is set by stop() and pause().
        Allocation cost drops from 2 Tasks + cleanup to 1 Task wrap
        (inside wait_for's ensure_future on wake_event.wait()).
        """
        assert self._stop_event is not None
        assert self._pause_event is not None
        assert self._wake_event is not None
        # Clear the wake signal before checking entry state. The single-
        # threaded asyncio model guarantees clear() + is_set() run as
        # one atomic block (no scheduler yield between them) so a stop()
        # or pause() that already fired is observed; a set() that lands
        # after the await begins resolves wait_for immediately.
        self._wake_event.clear()
        if self._stop_event.is_set() or self._pause_event.is_set():
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake_event.wait(), timeout=seconds)

    async def _wait_for_resume(self) -> None:
        """Block until resume_event is set OR stop_event is set.

        Used by the outer-while when a live takeover has paused the
        loop — we yield indefinitely (no rendering, no advancing) until
        the live session ends or the loop is asked to stop entirely.
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

        Used by Live takeover (SYSTEM_SPEC §5.11): when a live
        session activates, it pause()s the loop, takes over render_frame
        calls itself, then resume()s when the session ends. Idempotent —
        repeated pause() calls don't accumulate state.
        """
        if self._pause_event is None or self._resume_event is None:
            return
        self._resume_event.clear()
        self._pause_event.set()
        # Wake any in-flight _wait so live takeover yields promptly
        # rather than waiting for the current sleep to drain.
        if self._wake_event is not None:
            self._wake_event.set()

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
        """True when the injected renderer satisfies the IPC ops
        contract (begin_slide + advance + begin_transition + capture).

        Gates the IPC route. Duck-typed on `begin_slide` + `advance`
        so a test stub doesn't need to be a real RustRenderer
        subclass. Returns True for RustRenderer, AutoFallbackRenderer,
        and MockRenderer (DELETE-PIL slice 11 converted MockRenderer
        to the IPC shape so playback can route mock through the same
        rails as production)."""
        return hasattr(self._renderer, "begin_slide") and hasattr(self._renderer, "advance")

    def _maybe_kick_web_refresh(self, slide: WebSlide) -> None:
        """Fire-and-forget a screenshot refresh for `slide` if one is
        due — NON-BLOCKING.

        Called from `_loop` when a Web slide's slot is entered. A Web
        slide renders as an image slide (the renderer paints its
        `asset.png`); this keeps that asset fresh by periodically
        re-rendering a screenshot on-device.

        CRITICAL — this MUST NOT block the playback loop. An on-device
        render takes several seconds; if `_loop` awaited it the sign
        would freeze for it (the "coffe" Bug A failure class). So the
        render is launched with `asyncio.create_task` and this method
        returns IMMEDIATELY — `_loop` then plays the slide with
        whatever `asset.png` currently exists (last-good screenshot,
        or the create-time placeholder). The fresh screenshot lands
        asynchronously and shows up the next time the slot comes round
        (the renderer's image bake re-reads the PNG per paint).

        Guards:
          - no producer wired -> no-op.
          - not stale (web_refresh_due is False) -> no-op.
          - a fetch already in flight for this id -> no-op (the
            in-flight set prevents stacking tasks when a slot recurs
            faster than its fetch finishes).
        The done-callback clears the in-flight id and surfaces any
        task exception via the logger — the producer catches
        everything itself, so this is belt-and-suspenders.
        """
        if self._web_screenshot_producer is None:
            return
        now = asyncio.get_event_loop().time()
        last = self._web_last_fetch.get(slide.id)
        if not web_refresh_due(last, now, slide.refresh_interval_s):
            return
        self._launch_web_refresh(slide)

    def kick_web_refresh_now(self, slide: WebSlide) -> None:
        """Fire-and-forget an IMMEDIATE screenshot refresh for `slide`,
        bypassing the staleness check — NON-BLOCKING.

        Bug W1: a freshly-created (or url-changed) Web slide has only
        the synthetic placeholder asset until its first playback slot
        comes round — the dashboard thumbnail / editor preview are
        blank until then. The create/update API handler calls this so
        the real screenshot populates promptly instead of waiting for
        the periodic producer.

        Same non-blocking contract as `_maybe_kick_web_refresh`: the
        render is launched with `asyncio.create_task` and this returns
        IMMEDIATELY, so the create/update request never blocks on the
        on-device render. The in-flight guard still applies (a kick while
        a fetch is already running for this id is a no-op) and the
        kick time is stamped so the periodic loop's next slot sees the
        slide as fresh. A no-op if no producer is wired (test configs).
        """
        if self._web_screenshot_producer is None:
            return
        self._launch_web_refresh(slide)

    def _launch_web_refresh(self, slide: WebSlide) -> None:
        """Launch the fire-and-forget screenshot fetch task for `slide`.

        Shared by `_maybe_kick_web_refresh` (the periodic, staleness-
        gated path) and `kick_web_refresh_now` (the create/update
        immediate path). Stamps the kick time, marks the id in-flight,
        creates the task, and attaches the done-callback that clears
        the in-flight id + surfaces a crashed task via the logger.

        The in-flight guard lives here so neither caller can stack two
        fetches for the same slide. The producer is contracted never
        to raise; the done-callback is belt-and-suspenders so a
        failed task can't surface as an unretrieved-exception warning.
        """
        slide_id = slide.id
        if slide_id in self._web_inflight:
            return
        assert self._web_screenshot_producer is not None
        # Stamp the kick time NOW (not on completion): keeps the
        # refresh cadence steady regardless of how long the fetch
        # takes, and ensures a slow fetch isn't re-kicked next slot.
        self._web_last_fetch[slide_id] = asyncio.get_event_loop().time()
        self._web_inflight.add(slide_id)
        producer = self._web_screenshot_producer
        width = self._renderer.width
        height = self._renderer.height
        task = asyncio.create_task(producer(slide, width, height))

        def _on_done(t: "asyncio.Task[bool]", _id: UUID = slide_id) -> None:
            # Always clear the in-flight flag so the next due slot can
            # re-kick. The producer is contracted to never raise, but
            # guard anyway — a crashed task must not wedge the id.
            self._web_inflight.discard(_id)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                log.error(
                    "playback: web-screenshot task for slide id=%s crashed: %r",
                    _id,
                    exc,
                    exc_info=exc,
                )

        task.add_done_callback(_on_done)

    def _prune_web_tracking(self, items: list[ContentItem]) -> None:
        """Drop per-slide Web-refresh tracking entries for slide ids no
        longer in the current playlist — C3/M1 (2026-05-20).

        `_web_last_fetch` (a dict of last-kick timestamps) and
        `_web_inflight` (a set of ids with a fetch running) are both
        keyed by slide id. Without pruning, every Web slide id ever
        played accumulates an entry forever — an unbounded leak on a
        sign whose playlist churns through many Web slides over months.

        `_web_last_fetch` is pruned freely: it is just timestamps, and
        a re-added slide that has lost its entry simply reads as "due
        on first sight" (web_refresh_due treats a missing key as None),
        which is correct.

        `_web_inflight` is NOT pruned here. An id in `_web_inflight`
        means a fetch task is genuinely running for that slide; the
        kick's done-callback (`_on_done`) already removes the id when
        the task COMPLETES. Pruning a still-running id would re-enable
        a double-kick if that slide reappears before its fetch
        finishes. The set therefore self-cleans and needs no active
        prune — a stale entry is impossible because every task has a
        done-callback.
        """
        if not self._web_last_fetch:
            return
        current_ids = {item.id for item in items}
        stale = self._web_last_fetch.keys() - current_ids
        for slide_id in stale:
            del self._web_last_fetch[slide_id]

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
            # Bug 8 gap (2026-05-20): throttle per slide id. First skip
            # logs INFO; subsequent skips for the SAME id log DEBUG —
            # an all-unsupported playlist would otherwise spam this
            # line every loop pass.
            if item.id in self._skipped_slide_ids:
                log.debug(
                    "playback: skipping slide %s (unsupported kind; throttled): %s",
                    item.id,
                    e.message,
                )
            else:
                self._skipped_slide_ids.add(item.id)
                log.info(
                    "playback: skipping slide %s (Rust sidecar doesn't yet "
                    "support this kind; further skips for this id throttled "
                    "to DEBUG): %s",
                    item.id,
                    e.message,
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
                # first paint_slide). Skip gracefully — throttled per
                # slide id, same as the begin_slide skip above.
                if item.id in self._skipped_slide_ids:
                    log.debug(
                        "playback: slide %s unsupported mid-play (throttled): %s",
                        item.id,
                        e.message,
                    )
                else:
                    self._skipped_slide_ids.add(item.id)
                    log.info(
                        "playback: slide %s became unsupported mid-play: %s",
                        item.id,
                        e.message,
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
                    "playback: transition kind %r unsupported; falling through to instant cut: %s",
                    transition_kind,
                    e.message,
                )
                return True
            except RustRendererUnsupportedSlideError as e:
                # Round-23 attribution fix: begin_transition handed
                # next_item.id to the sidecar; if the sidecar can't
                # paint THAT slide (kind it doesn't know yet -- Stream,
                # future variant), pre-r23 the error escaped uncaught
                # to _loop's broad except which logged ERROR naming
                # the CURRENT slide (which played fine) and added
                # CURRENT's id to _failed_slide_ids, throttling later
                # real failures of the innocent current slide.
                #
                # Correct attribution: blame next_item.id, throttle
                # next_item.id in _skipped_slide_ids (same set the
                # current-slide unsupported-kind branch at line ~916
                # uses), return True so the current slide completes
                # cleanly. The outer iteration's begin_slide for the
                # next item will hit the existing skip path at L916
                # (which logs INFO first then DEBUG on repeats) --
                # but we add to _skipped_slide_ids here too so the
                # double-log doesn't fire (the L916 throttle check
                # short-circuits on the first encounter via this set).
                if next_item.id in self._skipped_slide_ids:
                    log.debug(
                        "playback: begin_transition to slide %s (unsupported kind; throttled): %s",
                        next_item.id,
                        e.message,
                    )
                else:
                    self._skipped_slide_ids.add(next_item.id)
                    log.info(
                        "playback: begin_transition target slide %s "
                        "is unsupported (Rust sidecar doesn't yet "
                        "support this kind; further skips for this id "
                        "throttled to DEBUG). Current slide played "
                        "fine -- outer iter will re-decide on the "
                        "next slide: %s",
                        next_item.id,
                        e.message,
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
                        "playback: transition %r failed mid-window; treating as cut: %s",
                        transition_kind,
                        e.message,
                    )
                    break
                except RustRendererUnsupportedSlideError as e:
                    # Round-23 attribution fix: same shape as the
                    # begin_transition catch above. Mid-window advance
                    # surfaced UnsupportedSlide -- target is next_item,
                    # not the current slide that played fine. Throttle
                    # per next_item.id in _skipped_slide_ids, break
                    # the transition window cleanly so the outer iter
                    # advances normally.
                    if next_item.id in self._skipped_slide_ids:
                        log.debug(
                            "playback: transition advance to slide %s unsupported (throttled): %s",
                            next_item.id,
                            e.message,
                        )
                    else:
                        self._skipped_slide_ids.add(next_item.id)
                        log.info(
                            "playback: transition advance surfaced "
                            "UnsupportedSlide for next slide %s "
                            "(further skips throttled to DEBUG): %s",
                            next_item.id,
                            e.message,
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

    async def _play_stream_slide(self, item: StreamSlide) -> bool:
        """Play a StreamSlide — Mode B of STREAM/VLC (proposal §6).

        Pumps the operator's network stream straight to the renderer for
        the slide's duration, bypassing the Rust sidecar (the sidecar's
        ContentItem enum has no stream kind). When the URL is
        unreachable, the stream ends early, or the renderer can't take
        external frames, the slide's on_unreachable policy fills the
        rest of the slot. Returns True if at least one frame rendered.

        Known limitations carried from the proposal:
        - No transition: a stream slot is a hard cut in and out. A
          real fade/wipe would need the sidecar's 2-input transition
          machinery, which this direct-render pump bypasses; the
          slide's transition fields are schema parity for now.
        - On the Rust sidecar render_frame() is not yet implemented
          (the push-frames IPC op is "slice 2.5"). The pump catches
          the first render failure, logs once, and falls back to
          on_unreachable — so a stream slide is a no-op slot on
          the real Pi until slice 2.5. Fully exercised on MockRenderer.
        - /api/playback/current-frame's capture reads the sidecar's
          last framebuffer, which the pump does not feed — a
          stream slot's live preview is therefore stale.
        """
        renderer = self._renderer
        loop = asyncio.get_event_loop()
        deadline = loop.time() + item.duration_ms / 1000.0
        consumer = StreamConsumer(item.stream_url, renderer.width, renderer.height)
        frames = consumer.frames()
        rendered_any = False
        renderer_broken = False
        # Outer try: end_external_frames() in its finally runs AFTER
        # every render_frame() call for this slot — the pump frames
        # AND any on_unreachable "black" frame. Ending pump-mode
        # before the on_unreachable paint would let that paint re-arm
        # a pump-mode session with no closer, hanging the sidecar.
        try:
            try:
                while True:
                    if self._stop_event.is_set() or self._pause_event.is_set():
                        break
                    now = loop.time()
                    if now >= deadline:
                        break
                    # Before the first frame the wait is bounded by
                    # the connect timeout (a dead URL must not eat the
                    # whole slot); after, by the remaining slot time.
                    if rendered_any:
                        budget = deadline - now
                    else:
                        budget = min(_STREAM_CONNECT_TIMEOUT_S, deadline - now)
                    try:
                        rgb = await asyncio.wait_for(frames.__anext__(), timeout=budget)
                    except StopAsyncIteration:
                        break  # ffmpeg EOF / stream disconnect
                    except TimeoutError:
                        break  # connect timeout, or the stream stalled
                    # HW-decode (2026-05-20): the consumer HW-decodes
                    # H.264 + emits SOURCE-resolution NV12 (no ffmpeg
                    # swscale — the GPU cover-fits). Thread the NV12
                    # format + source dims into render_frame() so the
                    # sidecar's begin_external_frames carries them.
                    # source_width/height are known after the
                    # consumer's ffprobe, i.e. before its first frame.
                    src_w = consumer.source_width
                    src_h = consumer.source_height
                    try:
                        renderer.render_frame(
                            rgb,
                            pixel_format=consumer.pixel_format,
                            frame_w=src_w,
                            frame_h=src_h,
                        )
                    except Exception:
                        # The renderer can't take external frames — a
                        # Rust sidecar without the slice-2.5 push-
                        # frames op. Log ONCE (per-frame logging at
                        # 30fps would flood the journal) and fall back
                        # to on_unreachable.
                        log.warning(
                            "playback: renderer rejected a stream "
                            "frame (slide id=%s) — the push-frames "
                            "sidecar op is unavailable; skipping the "
                            "rest of this slot",
                            item.id,
                        )
                        renderer_broken = True
                        break
                    rendered_any = True
            finally:
                await consumer.close()
                with contextlib.suppress(Exception):
                    await frames.aclose()
            # Fill the remainder of the slot if the stream never
            # delivered, ended before the deadline, or the renderer
            # rejected frames. Inside the outer try so a "black"
            # render_frame() is still covered by the finally below.
            if not (self._stop_event.is_set() or self._pause_event.is_set()):
                remaining = deadline - loop.time()
                if remaining > 0:
                    await self._apply_stream_on_unreachable(
                        item, remaining, renderer_broken=renderer_broken
                    )
        finally:
            # STREAM/VLC slice 2.5: end the sidecar's frame-pump
            # session AFTER every render_frame() for this slot, on
            # every exit path (deadline, EOF, stop, pause, render
            # failure) so the sidecar can't hang in pump-mode.
            try:
                renderer.end_external_frames()
            except Exception:
                log.exception(
                    "playback: end_external_frames failed for stream slide id=%s",
                    item.id,
                )
        return rendered_any

    async def _apply_stream_on_unreachable(
        self,
        item: StreamSlide,
        remaining_s: float,
        *,
        renderer_broken: bool = False,
    ) -> None:
        """Fill the rest of a StreamSlide's slot when its
        stream is unreachable / ended early / un-renderable. Applies
        the slide's on_unreachable policy (proposal Q12):

        - skip: return immediately so the loop advances now.
        - black: paint one black frame, then hold it for the
          remainder of the slot.
        - hold_last_frame (default): paint nothing — the framebuffer
          keeps whatever was last on screen — and wait the slot out.
        """
        if item.on_unreachable == "skip":
            return
        if item.on_unreachable == "black" and not renderer_broken:
            # All-zero RGB888 == black. Suppressed because the same
            # render path that just failed (renderer_broken) could
            # fail here too; the wait below still preserves slot
            # timing either way.
            #
            # HW-decode (2026-05-20): the stream pump runs in NV12
            # pixel format. The black fill is a renderer-sized RGB888 frame
            # — a different format. end_external_frames() closes any
            # active NV12 pump session first so the black frame opens
            # a fresh rgb888 session (begin_external_frames' format is
            # latched per-session). A no-op when no pump is active.
            with contextlib.suppress(Exception):
                self._renderer.end_external_frames()
            black = bytes(self._renderer.width * self._renderer.height * 3)
            with contextlib.suppress(Exception):
                self._renderer.render_frame(black)
        # hold_last_frame, or black after its paint: wait the slot
        # out. _wait returns early on stop/pause so a takeover still
        # preempts a held stream slot.
        await self._wait(remaining_s)

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
        re-uploading a valid video asset) gets ERROR-level visibility
        on the next attempt, not DEBUG-suppressed silence.

        Bug 8 gap (2026-05-20): the unsupported-kind skip throttle
        (_skipped_slide_ids) is cleared on the same event, for the
        same reason — a fixed/replaced slide should log fresh.
        """
        if playlist_id != self._current_playlist_id and (
            self._failed_slide_ids or self._skipped_slide_ids
        ):
            log.info(
                "playback: playlist changed (%s → %s); clearing %d failed "
                "+ %d skipped-slide throttle entries",
                self._current_playlist_id,
                playlist_id,
                len(self._failed_slide_ids),
                len(self._skipped_slide_ids),
            )
            self._failed_slide_ids.clear()
            self._skipped_slide_ids.clear()
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

        Otherwise: ask the renderer for a fresh PNG via the IPC
        `capture` op, store, return. Concurrent callers serialize
        behind a lock so a burst of requests issues exactly one
        capture; subsequent waiters get the freshly cached frame.

        Returns None when nothing is currently playing or capture
        otherwise fails (the API layer maps None to 503). The
        renderer owns on-screen state -- the sidecar paints into
        its own EGL session and the capture op writes that exact
        framebuffer to disk -- so this is the canonical snapshot
        primitive. No Python-side composition involved.
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
            elapsed_s = 0.0 if slot_t0 is None else max(0.0, loop.time() - slot_t0)
            try:
                png = await asyncio.to_thread(self._capture_current_frame_sync, elapsed_s)
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
        worker thread because the IPC capture roundtrip can stall on
        sidecar I/O and shouldn't block the event loop.

        Delegates to the renderer's `capture(path)` IPC op which writes
        a PNG containing whatever the sidecar last painted. We read the
        bytes back from the temp file and discard it. `elapsed_s` is
        kept in the signature for the caller's tracking but isn't
        threaded into the IPC op -- the sidecar's state machine is the
        source of truth for what's on screen, not a wall-clock anchor.
        """
        del elapsed_s  # see docstring -- sidecar owns timing now
        if self._current_id is None:
            return None
        if not hasattr(self._renderer, "capture"):
            return None
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tmp_path = Path(tf.name)
        try:
            try:
                self._renderer.capture(tmp_path)
            except Exception:
                log.exception("playback: renderer.capture failed")
                return None
            try:
                return tmp_path.read_bytes()
            except Exception:
                log.exception("playback: capture readback failed")
                return None
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()


def scheduled_fetch_items(
    content_storage: "ContentStorage",
    playlist_storage: "PlaylistStorage",
    schedule_storage: "ScheduleStorage",
    now: datetime,
) -> tuple[UUID, list[ContentItem]]:
    """Resolve the schedule-active playlist at `now` and return its items.

    Returns: (active_playlist_id, items in playlist order). Items have
    transitions patched from PlaylistItem (not the content model).

    Strict: items referenced by the playlist but absent from storage are
    silently dropped; orphan content NOT in the active playlist is excluded
    (so it won't leak onto the sign).

    Pure function -- no side effects. The dependencies.py closure that
    wraps this for the live PlaybackLoop takes the returned active_id and
    calls loop._stamp_playlist_id itself, keeping the throttle-clearing
    logic colocated with the loop and this helper trivially testable
    without instantiating PlaybackLoop + MockRenderer.

    Deferred imports inside the function dodge a content↔playlist↔schedule
    circular at module load.
    """
    from openmarquee.playlist import list_for_playback
    from openmarquee.schedule import evaluate_schedule

    schedule = schedule_storage.load()
    active_id = evaluate_schedule(now, schedule)
    items = list_for_playback(content_storage, playlist_storage, active_id)
    return active_id, items
