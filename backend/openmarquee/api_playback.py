"""REST API for the playback engine.

POST /api/playback/start — kick off the loop (no-op if already running)
POST /api/playback/stop  — stop and wait for the loop to exit
GET  /api/playback/state — { is_running, current_item_id, current_item_type,
                            current_item_transition, current_item_transition_ms,
                            current_item_auto_mode, current_item_auto_format,
                            current_playlist_id }
GET  /api/playback/perf/stats — renderer perf JSON sidecar. Perf-night
                            r2: surfaces the 30s ipc.soak window data
                            to the operator-facing UI perf overlay.
GET  /api/playback/loop_stats — Python playback-loop tick stats. Perf-
                            night r3: complementary to perf/stats —
                            answers "is Python the slow path?"

The loop drives the device's renderer (MockRenderer in dev, HUB75/HDMI/etc.
on the device once those land). Replaced the Phase 2 manual
/dev/play/{id} poke (the dev endpoint has since been removed).
"""

import json
import os
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ValidationError

from openmarquee.api import cors_headers_for_origin
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    get_content_storage,
    get_flock_storage,
    get_playback_loop,
    get_playlist_storage,
)
from openmarquee.flock import FlockStorage
from openmarquee.playback import PlaybackLoop
from openmarquee.playlist import PlaylistStorage

router = APIRouter(prefix="/api/playback", tags=["playback"])

# Perf-night r2 (2026-05-26): renderer emits the per-30s perf-stats
# JSON sidecar to this path (see renderer/src/ipc_main.rs
# PERF_STATS_JSON_PATH). Env-overridable so tests + dev runs can point
# at a tmp file. Mirrors the OPENMARQUEE_IDENTITY_PATH convention in
# identity.py:33-41.
DEFAULT_PERF_STATS_PATH = "/var/openmarquee/perf-stats.json"


def _perf_stats_path() -> Path:
    return Path(os.environ.get("OPENMARQUEE_PERF_STATS_PATH", DEFAULT_PERF_STATS_PATH))

LoopDep = Annotated[PlaybackLoop, Depends(get_playback_loop)]
ContentDep = Annotated[ContentStorage, Depends(get_content_storage)]
PlaylistDep = Annotated[PlaylistStorage, Depends(get_playlist_storage)]
FlockDep = Annotated[FlockStorage, Depends(get_flock_storage)]


class PlaybackState(BaseModel):
    is_running: bool
    current_item_id: UUID | None
    # "text_slide" | "image" | "video" | None — drives the live-preview
    # UI's choice of <video> vs <img>. Follows the ContentItem type
    # discriminator so adding a new content type auto-surfaces here.
    current_item_type: str | None
    # Transition that the *currently-displaying* PlaylistItem is
    # configured to perform on its way out ("cut" or "fade"). The live
    # preview keeps the last-seen pair in memory and replays a CSS
    # cross-fade when current_item_id changes, so the browser mirrors
    # what the device's playback loop does frame-by-frame.
    current_item_transition: str | None
    current_item_transition_ms: int | None
    # Auto-mode metadata for TextSlide items — drives the live preview's
    # client-side ticking overlay for time/date/day slides.
    current_item_auto_mode: str | None
    current_item_auto_format: str | None
    current_playlist_id: UUID | None


@router.get("/state", response_model=PlaybackState)
async def get_state(loop: LoopDep) -> PlaybackState:
    return PlaybackState(
        is_running=loop.is_running,
        current_item_id=loop.current_item_id,
        current_item_type=loop.current_item_type,
        current_item_transition=loop.current_item_transition,
        current_item_transition_ms=loop.current_item_transition_ms,
        current_item_auto_mode=loop.current_item_auto_mode,
        current_item_auto_format=loop.current_item_auto_format,
        current_playlist_id=loop.current_playlist_id,
    )


@router.get(
    "/current-thumbnail",
    response_class=FileResponse,
    responses={
        200: {"content": {"image/png": {}}},
        204: {"description": "Nothing is currently playing."},
        404: {"description": "Playlist's first item has no asset on disk."},
    },
)
async def get_current_thumbnail(
    request: Request,
    loop: LoopDep,
    content: ContentDep,
    playlists: PlaylistDep,
    flock_storage: FlockDep,
) -> Response:
    """Cover art for the playlist this device is currently playing —
    the first item of that playlist's asset, matching the playlist
    browser's convention (one stable visual per playlist).

    Drives the Flock panel's tile thumbnails. Using the current
    PLAYLIST's first slide (not the currently-displaying slide) keeps
    the tile visually stable as long as the same playlist is on — it
    only flips when the playlist itself changes (schedule rule kicks
    in, operator switches). Returns 204 when nothing is playing or
    the current playlist is empty.
    """
    if loop.current_item_id is None:
        # Nothing on screen → idle.
        return Response(status_code=204)

    playlist_id = loop.current_playlist_id
    first_id: UUID | None = None
    if playlist_id:
        playlist = playlists.get_by_id(playlist_id)
        if playlist is not None:
            ids = playlist.item_ids
            if ids:
                first_id = ids[0]
    # Fall back to the slide on screen so the tile never goes blank
    # during a race between a rename/delete and the next playback tick.
    if first_id is None:
        first_id = loop.current_item_id

    path = content.asset_path(first_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no asset for {first_id}")
    # Batch 11.3 / sweep #5 #4: CORS allowlist-reflective. Echo back the
    # Origin only when it's in the operator's flock allowlist (or a
    # builtin local origin). Same-origin requests don't carry Origin so
    # they fall through cleanly without any ACAO header (browser doesn't
    # need one).
    headers = {"Cache-Control": "no-store"}
    headers.update(cors_headers_for_origin(request.headers.get("origin", ""), flock_storage))
    return FileResponse(
        path,
        media_type="image/png",
        headers=headers,
    )


@router.get(
    "/current-frame",
    responses={
        200: {"content": {"image/png": {}}},
        503: {
            "description": "Capture not available (nothing playing or non-text/non-image slide)."
        },
    },
)
async def get_current_frame(
    request: Request,
    loop: LoopDep,
    flock_storage: FlockDep,
) -> Response:
    """PNG of what's actually rendering right now -- the slide's live
    composite at current elapsed_s, NOT the playlist cover art that
    /current-thumbnail returns. Distinct from /current-thumbnail in
    that it reflects motion + auto-mode (clock ticks, ticker
    position, etc.) at the captured moment.

    Backed by an in-memory cache in the playback loop with a 5-minute
    TTL plus immediate playlist-change invalidation. The cache caps
    capture cost at ~one renderer-capture IPC roundtrip per 5 minutes
    per distinct playlist; concurrent callers serialize behind a single
    lock so a burst of requests issues at most one capture.

    Returns 503 when nothing is playing OR the current slide type
    has no readback path (video). On error we honestly fail rather
    than fall back to the playlist cover -- the whole point of this
    endpoint is "real frame, or nothing." (Use /current-thumbnail if
    cover art is what the caller actually wants.)
    """
    png = await loop.cached_current_frame_png()
    if png is None:
        return Response(
            status_code=503,
            content="capture not available",
            media_type="text/plain",
        )
    # Batch 11.3 / sweep #5 #4: same allowlist-reflective ACAO as
    # /current-thumbnail above. See cors_headers_for_origin in api.py.
    headers = {"Cache-Control": "no-store"}
    headers.update(cors_headers_for_origin(request.headers.get("origin", ""), flock_storage))
    return Response(
        content=png,
        media_type="image/png",
        headers=headers,
    )


@router.post("/start", status_code=204)
async def start_playback(loop: LoopDep) -> None:
    await loop.start()


@router.post("/stop", status_code=204)
async def stop_playback(loop: LoopDep) -> None:
    await loop.stop()


# Perf-night r2 (2026-05-26): operator-facing perf overlay surface.
# The renderer's IPC summary path (ipc_main.rs:maybe_emit_summary) drops
# this JSON to /var/openmarquee/perf-stats.json every 30s alongside the
# `ipc.soak` eprintln. Wire shape mirrors PerfStatsJson in
# renderer/src/ipc_main.rs — renaming a field here without matching the
# Rust side will break the operator-facing UI overlay silently.


class RendererPerfStats(BaseModel):
    # 30s window the renderer last emitted. NOT a sliding window — the
    # renderer resets its counters at each emit; the UI computes
    # window-deltas itself from consecutive polls if it wants a finer
    # cadence than 30s.
    window_s: int
    frames: int
    transitions: int
    fps_avg: float
    paint_us_avg: int
    paint_us_max: int
    paint_us_p99: int
    session_frames: int
    session_transitions: int
    # Session-cumulative deadline-miss counters from r1
    # (renderer/src/hdmi.rs EglSession::record_present). Monotonically
    # non-decreasing across the session lifetime; UI can diff
    # consecutive polls to compute a per-window over-budget rate.
    frames_observed_total: int
    frames_over_budget_total: int
    # Wall-clock unix epoch seconds of the renderer-side write. UI uses
    # this to detect staleness (renderer crashed, journald drift, etc.).
    # Backend doesn't gate on staleness — the operator sees both the
    # data and the age and makes the call.
    timestamp_unix_s: int


class PythonLoopStats(BaseModel):
    """Perf-night r3 (2026-05-26): Python playback-loop tick stats.

    Complementary to /api/playback/perf/stats (renderer-side counters):
    that one says "is Rust meeting 30fps?"; this one says "is Python's
    playback loop staying under its 33ms tick budget?".

    A high `ticks_over_budget` here points at Python-side blocking
    (sync I/O on the tick path, slow JSON load, sync IPC readline)
    rather than renderer-side paint cost. The dispatch's r4 candidate
    is the asyncio.to_thread wrap of `_send_op` — if these numbers
    show consistent tick > 5ms, that's the fix.

    All time fields are MICROSECONDS (the inner ring stores nanoseconds
    for measurement precision; the endpoint exposes us for operator
    readability — same convention as the renderer's paint_us_p99).
    `ticks_over_budget` is the count whose work-time exceeded 33000us.
    """

    ticks_observed: int
    p50_us: int
    p95_us: int
    p99_us: int
    max_us: int
    ticks_over_budget: int


@router.get("/loop_stats", response_model=PythonLoopStats)
async def get_loop_stats(loop: LoopDep) -> PythonLoopStats:
    """Snapshot of the playback loop's per-tick work-time ring buffer.

    Returns all-zero when the ring is empty (no playback has run yet
    since this PlaybackLoop instance was constructed — common at boot
    or in a freshly-spawned dev process).

    Auth-gated by AuthMiddleware (auth_middleware.py:141-) like every
    /api/* route not on the whitelist. The whitelist (auth_middleware.py:
    50-93) carves out unauthenticated routes; /api/playback/loop_stats
    is NOT on it.
    """
    return PythonLoopStats(**loop.get_loop_stats())


# r15 (2026-05-26) robustness: cap perf-stats.json read at 64 KB.
# The emitted JSON is a fixed 12-field shape that serializes to <1 KB
# in practice (mostly small integers + a couple of f64s). A defensive
# 64× headroom guards against:
#   - renderer-side bug emitting an unexpectedly large payload
#   - file replaced by a non-renderer writer (root-shell mistake, disk
#     corruption that doubles content, etc.)
# Without the cap, `path.read_text()` would happily allocate gigabytes
# into the request handler before Pydantic gets to reject the schema.
_PERF_STATS_MAX_BYTES = 65536


@router.get("/perf/stats", response_model=RendererPerfStats)
async def get_perf_stats() -> RendererPerfStats:
    """Return the latest perf-stats sidecar emitted by the renderer.

    503 cases:
      - renderer hasn't emitted its first ipc.soak window yet
        (file missing — first 30s after session start)
      - file unreadable (permissions, FS error)
      - file exceeds `_PERF_STATS_MAX_BYTES` (defensive cap; the
        emitted JSON is <1 KB in practice)
      - JSON parse failed (corrupted write — shouldn't happen with the
        atomic-write helper but defended against)
      - schema mismatch (renderer side renamed a field without backend
        catching up)
    """
    path = _perf_stats_path()
    try:
        st = path.stat()
    except FileNotFoundError:
        # `from None`: the missing-file case is operator-expected
        # (first 30s of session before the renderer's first ipc.soak
        # emit). Suppress the FileNotFoundError chain so the response
        # body stays clean.
        raise HTTPException(
            status_code=503,
            detail=(
                "perf stats sidecar not yet written "
                "(renderer hasn't emitted an ipc.soak window yet)"
            ),
        ) from None
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"perf stats sidecar stat failed: {exc}",
        ) from exc
    if st.st_size > _PERF_STATS_MAX_BYTES:
        # Defensive cap: refuse to read a file that's larger than
        # any reasonable renderer emit. The renderer's typical write
        # is <1 KB; anything past 64 KB is a bug somewhere upstream
        # OR an attacker-replaced file. Either way we don't want the
        # handler to allocate it just to find out.
        raise HTTPException(
            status_code=503,
            detail=(
                f"perf stats sidecar size {st.st_size} bytes exceeds "
                f"the {_PERF_STATS_MAX_BYTES}-byte cap "
                "(corrupted or attacker-replaced file)"
            ),
        )
    try:
        raw = path.read_text()
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"perf stats sidecar read failed: {exc}",
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"perf stats sidecar parse failed: {exc}",
        ) from exc
    try:
        return RendererPerfStats(**data)
    except ValidationError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"perf stats sidecar schema mismatch: {exc}",
        ) from exc
