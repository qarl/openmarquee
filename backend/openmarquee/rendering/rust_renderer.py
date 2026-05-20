"""Phase 7 slice 1 (2026-05-13): Python proxy for the Rust IPC sidecar.

Subprocess-launches `openmarquee-render --ipc-sidecar`, owns its stdin /
stdout pipes, and exposes the 7-op IPC contract (`open` / `begin_slide` /
`advance` / `begin_transition` / `capture` / `reconfigure` / `close`) as
typed Python methods.

## Wire format

Stdin / stdout, JSON lines (one request per line, one response per line).
Request-response synchronous: each `_send_op` writes one request and reads
one response before returning.

Request shape (externally tagged via `op`, params payload in `params`):
    {"op": "<name>", "params": {<per-op fields>}}
    {"op": "close"}                                    # no params

Response shape (externally tagged via `ok` / `err`, OpResult internally
tagged via `command`):
    {"ok": {"result": {"command": "<variant>", <fields>}}}
    {"err": {"error": "<wire-format string>"}}

NOTE: `docs/renderer-rewrite-plan-rust.md` §7 describes UDS +
length-prefixed bincode + a periodic `Health` message. None of that is
in the actual Rust sidecar implementation (`renderer/src/ipc_main.rs` +
`renderer/src/playback.rs`). The doc has drifted; this proxy matches
what the code actually emits.

## Relationship to the Renderer Protocol

The Rust sidecar owns GPU-side composition; frame bytes never cross the
process boundary. `RustRenderer.render_frame` raises `NotImplementedError`
— it exists only to satisfy the `Renderer` Protocol's nominal shape so a
dependency injection that types as `Renderer` doesn't have to special-case
us at the type level. Real callers (slice 4's playback.py bypass) will
use the IPC ops directly.

## Failure model (slice 1 baseline + 2026-05-14 reconnect/watchdog)

- Subprocess fails to launch: `RustRendererSubprocessError`.
- Subprocess dies mid-session: the proxy attempts up to
  `reconnect_max_retries` (default 3) re-launch-and-re-open cycles
  within a `reconnect_window_s` (default 60s) rolling window. On
  success, the failing op raises `RustRendererRespawnedError`
  (a `RustRendererSubprocessError` subclass) — caller must replay
  any per-session state (begin_slide / begin_transition). On
  exhaustion, `RustRendererSubprocessError` is raised with the trail
  of exit codes / reasons in the message.
- Sidecar returns `{"err": {"error": "..."}}`: `RustRendererOpError`
  with `.message` = the wire-format error string (byte-stable per
  `renderer/src/ipc_main.rs` cargo tests at commit 601820f). Match
  against `.message` for error-class dispatch.
- Malformed JSON or unknown variant: `RustRendererProtocolError`.

## Liveness + Health

The Rust sidecar does NOT implement a Health op (the doc-spec'd
periodic Health message in `docs/renderer-rewrite-plan-rust.md` §7
has drifted from `renderer/src/ipc_main.rs`'s actual op set).
Liveness derives from two sources:

- `is_alive` (property): synchronous `subprocess.poll()` check.
- `health_probe()` (method): returns a `HealthState` snapshot of
  liveness + exit code + reconnect history. On-demand only — there
  is no server-driven heartbeat.

A background watchdog thread (1Hz by default; configurable via
`watchdog_interval_s`) polls liveness and triggers the same
reconnect path on detected death. The watchdog can be disabled by
passing `watchdog_enabled=False`.

## Thread safety

IPC is request-response synchronous; `_send_op` is guarded by an
internal lock so concurrent callers serialize cleanly. The watchdog
thread tries the lock non-blocking so a slow op never deadlocks the
watchdog (it just defers reconnect to the next tick). The stderr
reader thread drains the subprocess's stderr pipe to prevent deadlock
when the subprocess logs more than the pipe-buffer worth of stderr.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# ============================================================
# Typed exception hierarchy.
# ============================================================


class RustRendererError(Exception):
    """Base class for all RustRenderer errors."""


class RustRendererSubprocessError(RustRendererError):
    """The renderer subprocess failed to launch, died, or otherwise
    misbehaved at the process layer (broken pipes, EOF, non-zero exit
    before response).
    """


class RustRendererRespawnedError(RustRendererSubprocessError):
    """The subprocess died mid-op and the proxy successfully auto-
    reconnected. The failing op is NOT retried — session state
    (begin_slide / begin_transition) is lost on the new subprocess
    and must be replayed by the caller. Subsequent ops on the proxy
    will run against the fresh subprocess.

    Subclass of `RustRendererSubprocessError` so callers that
    broad-except the latter still catch this; callers that want to
    distinguish a transient respawn from a permanent failure can
    catch `RustRendererRespawnedError` first.
    """


class RustRendererProtocolError(RustRendererError):
    """The wire protocol was violated: invalid JSON, missing required
    fields, unknown response variant. Indicates a doc / impl drift or
    a corrupted pipe.
    """


class RustRendererOpError(RustRendererError):
    """The sidecar returned `{"err": {"error": "..."}}` to an op.

    `.message` is the verbatim error string from the sidecar. These
    strings are byte-stable per the cargo tests in
    `renderer/src/ipc_main.rs` (commit 601820f; updated piece 3e).
    Match against them for error-class dispatch:

      - "paint_slide: image_slide requires content_root (--content-root)"
      - "paint_transition: from non-text slide TBD"
      - "paint_transition: to non-text slide TBD"
      - "Capture: VideoSlide capture not implemented (image + text only)"
        (V4L2 piece 3e: paint_slide for Video now renders; Capture
        for Video remains a separate arc — Video screenshots /
        thumbnails out of scope. The marker uses its own distinct
        substring so paint_slide failures can't be misclassified
        as the deferred Capture path.)

    Subclass `RustRendererUnsupportedSlideError` covers the slide-kind-
    not-implemented cases (today: non-text transition; Capture of
    Video). The proxy promotes those at `_decode_response` so callers
    can dispatch on type rather than parsing the message.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class RustRendererUnsupportedSlideError(RustRendererOpError):
    """The sidecar refused an op because the slide kind isn't supported
    yet (today: VideoSlide -- V4L2 M2M + dmabuf import are task #76).

    Distinct from generic RustRendererOpError so AutoFallbackRenderer
    treats "this slide doesn't fit through the Rust route" differently
    from "the proxy is busted" -- the former lets the playback loop
    skip the slide and continue on the Rust route; the latter swaps
    the process over to MockRenderer.

    Promoted at the proxy boundary (`_decode_response`) by matching the
    sidecar's byte-stable wire-format error strings. Subclass of
    `RustRendererOpError` so callers that broad-except the latter still
    catch this; callers that want to distinguish unsupported-content
    from generic op-errors catch this first.

    Wire-format substrings that promote to this class:
      - "VideoSlide capture not implemented"  (Capture only;
                                                paint_slide ships)
      - "non-text slide TBD"                  (paint_transition from/to)

    Do not re-word the matched substrings without bumping the cargo
    tests in `renderer/src/ipc_main.rs` in lockstep.
    """


class RustRendererUnsupportedTransitionError(RustRendererOpError):
    """The sidecar refused a `begin_transition` op because the requested
    transition kind isn't yet wired in the shader pipeline.

    Parallel to `RustRendererUnsupportedSlideError`: distinguishes "this
    transition kind isn't implemented" from "the proxy is busted" so
    AutoFallbackRenderer can let playback log + fall through to an
    instant-cut for the one transition, without swapping the whole
    process over to MockRenderer.

    Forward-compat: as of 2026-05-14, `hdmi_logic::fs_for_transition_kind`
    accepts ALL 16 known kinds (cut + the 15 named in
    playback._SHADER_TRANSITION_KINDS) and silently FS_CUT-fallbacks
    for anything else (see `paint_and_present_one_transition_frame`).
    The proxy can't promote what the sidecar doesn't emit, so this
    exception class never fires TODAY. It exists for the case where
    a future Python schema adds a kind that hasn't been shader-wired
    AND a future Rust change starts emitting an explicit error for
    unknown kinds (e.g. "paint_transition: unknown kind 'foo'").

    Wire-format substring that promotes to this class (provisional;
    Rust doesn't emit this today):
      - "transition kind not implemented"
      - "unknown transition kind"
    """


# Closed 2026-05-14 (this commit): the legacy "video slides TBD"
# substring is gone from the tuple. VideoSlide paint is wired
# through V4L2 M2M H.264 decode + GLES BT.601 NV12 shader (pieces
# 3 + 4); Capture-side still emits its own distinct marker for
# VideoSlide screenshots / thumbnails (separate arc). The
# Capture marker now uses a non-overlapping substring so a real
# paint_slide failure (asset.mp4 corrupt, codec absent, frame
# upload failure) can NEVER be misclassified as the deferred
# Capture path -- those failures fall through to bare
# RustRendererOpError as a hard render failure (no fallback),
# matching the Image-slide / Text-slide failure shape.
_UNSUPPORTED_SLIDE_WIRE_MARKERS: tuple[str, ...] = (
    "VideoSlide capture not implemented",
    "non-text slide TBD",
    # Bug 8 / Fix A (2026-05-17): rust sidecar's cache.load skip-
    # marker rail. When an MP4 demuxer fails to open (multi-trak,
    # malformed, missing), BeginSlide returns an err carrying this
    # substring so _classify_op_error promotes to UnsupportedSlide
    # and _play_via_rust_ipc's existing skip path handles it (log
    # INFO + return False + outer loop advances). Without this, a
    # bad video hot-spun the loop with ERROR tracebacks (frozen-
    # sign incident @ 192.168.1.67).
    "video slide unsupported (load failed)",
)


_UNSUPPORTED_TRANSITION_WIRE_MARKERS: tuple[str, ...] = (
    # Provisional markers. Rust currently FS_CUT-fallbacks silently;
    # if a future change emits explicit errors for unknown kinds, the
    # substrings should match one of these. Drift-tolerant: if Rust
    # picks a different phrasing, update both ends in lockstep.
    "transition kind not implemented",
    "unknown transition kind",
)


def _classify_op_error(message: str) -> RustRendererOpError:
    """Pick the most specific `RustRendererOpError` subclass for a
    sidecar-emitted error string. Default is bare `RustRendererOpError`."""
    for marker in _UNSUPPORTED_SLIDE_WIRE_MARKERS:
        if marker in message:
            return RustRendererUnsupportedSlideError(message)
    for marker in _UNSUPPORTED_TRANSITION_WIRE_MARKERS:
        if marker in message:
            return RustRendererUnsupportedTransitionError(message)
    return RustRendererOpError(message)


# ============================================================
# Typed result objects. Returned by the corresponding IPC ops.
# Use isinstance() to discriminate (no Literal-tag field — see
# the field-default ordering trap with mixed-default dataclasses).
# ============================================================


@dataclass(frozen=True)
class OpenResult:
    """Return value of `open()`. The mode_w / mode_h are the sidecar's
    negotiated output dims (which may differ from the requested dims —
    e.g., HDMI mode-set lands on the panel's native res)."""

    mode_w: int
    mode_h: int


@dataclass(frozen=True)
class PaintSlide:
    """`advance()` result: paint the current slide at `t_in_slide_ms`."""

    slide_id: uuid.UUID
    t_in_slide_ms: int


@dataclass(frozen=True)
class PaintTransition:
    """`advance()` result: paint the blend at normalized `progress`.

    `from_id` mirrors the wire-format `from` field (Python reserved word
    handling)."""

    from_id: uuid.UUID
    to: uuid.UUID
    kind: str
    progress: float


@dataclass(frozen=True)
class SlideComplete:
    """`advance()` result: slide duration elapsed. Caller should
    begin_transition or begin_slide before the next advance produces
    useful output."""

    slide_id: uuid.UUID


@dataclass(frozen=True)
class Idle:
    """`advance()` result: no slide loaded yet."""


AdvanceResult = PaintSlide | PaintTransition | SlideComplete | Idle


@dataclass(frozen=True)
class CaptureResult:
    """`capture()` result: PNG written to `path` (size in bytes)."""

    path: str
    bytes: int


@dataclass(frozen=True)
class HealthState:
    """`health_probe()` result: on-demand liveness snapshot.

    The Rust sidecar has no Health op (the doc-spec'd periodic
    heartbeat in `docs/renderer-rewrite-plan-rust.md` §7 isn't
    implemented in `renderer/src/ipc_main.rs`'s 7-op set). This
    state derives from `subprocess.poll()` and the proxy's own
    reconnect bookkeeping.

    `is_alive`: True iff the subprocess is still running.
    `exit_code`: returncode if dead, None if alive or never started.
    `reconnect_attempts_in_window`: count of reconnect tries the
       proxy has made within the current rolling window (default 60s).
    `reconnect_history`: human-readable trail of recent reconnect
       reasons (most-recent-last), bounded to the window.
    """

    is_alive: bool
    exit_code: int | None
    reconnect_attempts_in_window: int
    reconnect_history: tuple[str, ...] = field(default_factory=tuple)


# ============================================================
# The proxy itself.
# ============================================================


class RustRenderer:
    """IPC proxy for `openmarquee-render --ipc-sidecar`.

    Use as a context manager so the subprocess is torn down even if the
    caller raises mid-session::

        with RustRenderer(width=1920, height=1080, content_root="/var/openmarquee/content") as r:
            r.begin_slide(slide_id, t0_ms=0, duration_ms=5000)
            cmd = r.advance(t_ms=100)
            ...

    Or driven manually with `open()` / `close()`::

        r = RustRenderer(width=1920, height=1080, ...)
        r.open()
        try:
            ...
        finally:
            r.close()
    """

    DEFAULT_BINARY = "/usr/local/bin/openmarquee-render"
    # Graceful-exit wait before terminate(); terminate() wait before kill().
    GRACEFUL_EXIT_TIMEOUT_S = 5.0
    TERMINATE_TIMEOUT_S = 2.0
    # Reconnect defaults. The 3-retries-in-60s window matches typical
    # process-supervisor crash-loop thresholds (e.g. systemd Restart=
    # on-failure with StartLimitBurst=3 / StartLimitIntervalSec=60s).
    DEFAULT_RECONNECT_MAX_RETRIES = 3
    DEFAULT_RECONNECT_WINDOW_S = 60.0
    DEFAULT_WATCHDOG_INTERVAL_S = 1.0

    def __init__(
        self,
        *,
        width: int,
        height: int,
        binary_path: str | os.PathLike[str] | None = None,
        content_root: str | os.PathLike[str] | None = None,
        drm_card: str | None = None,
        output: str = "hdmi",
        extra_args: list[str] | None = None,
        get_timezone: Callable[[], str | None] | None = None,
        reconnect_max_retries: int = DEFAULT_RECONNECT_MAX_RETRIES,
        reconnect_window_s: float = DEFAULT_RECONNECT_WINDOW_S,
        watchdog_enabled: bool = True,
        watchdog_interval_s: float = DEFAULT_WATCHDOG_INTERVAL_S,
    ):
        # Renderer Protocol attrs. Refreshed from the sidecar's
        # OpenOk response after open() so the negotiated mode wins
        # if the panel sets a different res than requested.
        self.width = int(width)
        self.height = int(height)

        self._binary_path = str(
            binary_path
            or os.environ.get("OPENMARQUEE_RENDERER_BINARY", self.DEFAULT_BINARY)
        )
        self._content_root = str(content_root) if content_root is not None else None
        self._drm_card = drm_card
        self._output = output
        self._extra_args = list(extra_args or [])
        # Bug 1 follow-up (2026-05-20): resolves the operator's
        # configured IANA timezone (settings.timezone) so the
        # sidecar's auto_mode clock renders local time. Read at
        # each (re)spawn — a Settings tz change followed by a
        # backend restart re-spawns the sidecar with the new TZ.
        self._get_timezone = get_timezone or (lambda: None)

        self._proc: subprocess.Popen[str] | None = None
        self._stderr_thread: threading.Thread | None = None
        # STREAM/VLC slice 2.5: the dedicated binary frame channel.
        # _frame_pipe is the write end of a pipe whose read end the
        # sidecar inherits; render_frame() pushes length-prefixed
        # RGB888 frames down it. _external_frames_active tracks
        # whether we have sent the begin_external_frames op (whose
        # JSON response is deferred until end_external_frames()).
        self._frame_pipe: object | None = None  # BufferedWriter
        self._external_frames_active = False
        # RLock so reconnect can call _send_op (which re-acquires the
        # lock) from within an already-locked _send_op scope without
        # deadlocking. Watchdog is a separate thread and uses
        # acquire(blocking=False); RLock is per-thread reentrant so a
        # different thread holding the lock still blocks the watchdog
        # from acquiring — which is the behavior we want.
        self._lock = threading.RLock()
        self._opened = False

        # Reconnect bookkeeping.
        self._reconnect_max = int(reconnect_max_retries)
        self._reconnect_window_s = float(reconnect_window_s)
        # Attempt timestamps (monotonic seconds); pruned to the rolling
        # window each time we inspect the count.
        self._reconnect_attempts: list[float] = []
        # Capped trail of human-readable reasons; kept aligned with
        # _reconnect_attempts after pruning.
        self._reconnect_reasons: list[str] = []

        # Watchdog.
        self._watchdog_enabled = bool(watchdog_enabled)
        self._watchdog_interval_s = float(watchdog_interval_s)
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle.
    # ------------------------------------------------------------------

    def __enter__(self) -> RustRenderer:
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            self.close()
        except Exception:
            log.exception("RustRenderer.close() failed during __exit__")

    def open(self) -> OpenResult:
        """Op 1. Launch the subprocess + send Open. Idempotent-when-already-
        opened: raises if called twice on the same instance."""
        if self._proc is not None:
            raise RustRendererError("RustRenderer.open() called twice")
        self._launch_subprocess()
        try:
            mode_w, mode_h = self._send_open_op()
            self.width = mode_w
            self.height = mode_h
            self._opened = True
            self._start_watchdog()
            return OpenResult(mode_w=mode_w, mode_h=mode_h)
        except Exception:
            # Open failed; tear down so the next open() can re-launch
            # without confusion.
            self._terminate_subprocess()
            raise

    def _send_open_op(self) -> tuple[int, int]:
        """Send the Open IPC op and decode the negotiated mode. Used by
        both the user-facing open() and the reconnect path."""
        params: dict[str, Any] = {"output": self._output}
        if self._drm_card is not None:
            params["drm_card"] = self._drm_card
        if self._content_root is not None:
            params["content_root"] = self._content_root
        body = self._send_op("open", params, _allow_reconnect=False)
        if body is None or "mode_w" not in body or "mode_h" not in body:
            raise RustRendererProtocolError(
                f"Open OK response missing mode_w/mode_h: {body!r}"
            )
        return int(body["mode_w"]), int(body["mode_h"])

    def begin_slide(
        self, slide_id: uuid.UUID | str, t0_ms: int, duration_ms: int
    ) -> None:
        """Op 2. Loads the slide into the sidecar's cache + resets per-slide
        playback state."""
        self._send_op(
            "begin_slide",
            {
                "slide_id": str(slide_id),
                "t0_ms": int(t0_ms),
                "duration_ms": int(duration_ms),
            },
        )

    def advance(self, t_ms: int) -> AdvanceResult:
        """Op 3. Tells the sidecar to paint a frame at wall-clock `t_ms`.
        Returns one of PaintSlide / PaintTransition / SlideComplete / Idle."""
        body = self._send_op("advance", {"t_ms": int(t_ms)})
        return self._parse_advance_result(body)

    def begin_transition(
        self,
        to_slide_id: uuid.UUID | str,
        to_duration_ms: int,
        kind: str,
        transition_ms: int,
        t0_ms: int,
    ) -> None:
        """Op 4. Stages the next slide + starts the blend window. Subsequent
        advance() calls drive the per-frame paint."""
        self._send_op(
            "begin_transition",
            {
                "to_slide_id": str(to_slide_id),
                "to_duration_ms": int(to_duration_ms),
                "kind": kind,
                "transition_ms": int(transition_ms),
                "t0_ms": int(t0_ms),
            },
        )

    def capture(self, path: str | os.PathLike[str]) -> CaptureResult:
        """Op 5. Re-paint the current scene and write a PNG to `path`.
        Caller is responsible for the directory existing + write permissions."""
        body = self._send_op("capture", {"path": str(path)})
        if body is None or "path" not in body or "bytes" not in body:
            raise RustRendererProtocolError(
                f"Capture OK response missing path/bytes: {body!r}"
            )
        return CaptureResult(path=str(body["path"]), bytes=int(body["bytes"]))

    def reconfigure(
        self,
        *,
        rotation: int | None = None,
        brightness: float | None = None,
        gamma: float | None = None,
    ) -> None:
        """Op 6. Apply new settings without losing playback state.

        Note: the current sidecar returns `"Reconfigure not yet implemented
        (slice e)"` for any reconfigure call (per `ipc_main.rs`). The proxy
        will surface that as `RustRendererOpError` until the slice lands."""
        params: dict[str, Any] = {}
        if rotation is not None:
            params["rotation"] = int(rotation)
        if brightness is not None:
            params["brightness"] = float(brightness)
        if gamma is not None:
            params["gamma"] = float(gamma)
        self._send_op("reconfigure", params)

    def close(self) -> None:
        """Op 7. Send Close + tear down the subprocess. Safe to call repeatedly;
        the second + subsequent calls are no-ops."""
        # Stop the watchdog BEFORE teardown so it doesn't try to reconnect
        # the very subprocess we're tearing down.
        self._stop_watchdog()
        if self._proc is None:
            return
        try:
            if self._opened:
                try:
                    # Don't auto-reconnect during close — if the
                    # subprocess is already dead, just tear down.
                    self._send_op("close", None, _allow_reconnect=False)
                except RustRendererSubprocessError:
                    # Subprocess already gone — fine, we're tearing
                    # down anyway. Log at debug.
                    log.debug("close: subprocess gone before Close op acknowledged")
                self._opened = False
        finally:
            self._terminate_subprocess()

    # ------------------------------------------------------------------
    # Renderer Protocol conformance (nominal).
    # ------------------------------------------------------------------

    def render_frame(self, frame: bytes) -> None:
        """Push one RGB888 frame to the sidecar (STREAM/VLC slice 2.5).

        `frame` is row-major RGB888, `width * height * 3` bytes. The
        first call lazy-sends the `begin_external_frames` op (a normal
        request/response op the sidecar acks immediately, flipping
        itself into pump-mode); every call then writes the frame
        length-prefixed onto the dedicated binary frame channel and
        the sidecar paints it fullscreen.

        Source-agnostic: this is "an RGB frame from some producer" —
        used by the VLC takeover + VlcStreamSlide pumps today, and the
        future webpage-slide path (STREAM_VLC_PROPOSAL §10).
        """
        with self._lock:
            if self._proc is None or self._frame_pipe is None:
                raise RustRendererError("RustRenderer not opened")
            if not self._external_frames_active:
                # Lazy-begin: flip the sidecar into pump-mode. This is
                # a normal write+read op — the sidecar acks at once,
                # so a sidecar that can't pump-render surfaces its
                # error here rather than desyncing the channel.
                # _send_op_locked (not _send_op) is deliberate: it
                # runs under the already-held _lock, and the auto-
                # reconnect path is bypassed — a mid-pump reconnect
                # would lose pump state anyway, and the VLC pumps
                # already catch the raised error.
                self._send_op_locked(
                    "begin_external_frames",
                    {"width": self.width, "height": self.height},
                )
                self._external_frames_active = True
            header = len(frame).to_bytes(4, "big")
            try:
                self._frame_pipe.write(header)
                self._frame_pipe.write(frame)
                self._frame_pipe.flush()
            except (BrokenPipeError, OSError) as e:
                raise RustRendererSubprocessError(
                    f"frame-channel write failed: {e}"
                ) from e

    def end_external_frames(self) -> None:
        """End a run of `render_frame()` pushes (STREAM/VLC slice 2.5).

        Writes the 0-length end sentinel onto the binary frame channel
        — which returns the sidecar to its JSON-op loop. There is no
        response: the begin op was already acked, and the sentinel
        carries no reply.

        Idempotent and a no-op when no run is active: the VLC pumps
        call this on every exit path (stop / pause / deadline / EOF),
        including ones that pushed zero frames.
        """
        with self._lock:
            if not self._external_frames_active:
                return
            self._external_frames_active = False
            if self._frame_pipe is None:
                return
            try:
                self._frame_pipe.write((0).to_bytes(4, "big"))
                self._frame_pipe.flush()
            except (BrokenPipeError, OSError) as e:
                raise RustRendererSubprocessError(
                    f"frame-channel sentinel write failed: {e}"
                ) from e

    # ------------------------------------------------------------------
    # Liveness.
    # ------------------------------------------------------------------

    @property
    def is_alive(self) -> bool:
        """True iff the subprocess is still running. Fast and non-blocking.

        Use this in place of a Health-op poll — the doc-spec'd periodic
        Health message doesn't exist in the actual sidecar implementation."""
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------
    # Internals.
    # ------------------------------------------------------------------

    def _launch_subprocess(self) -> None:
        # extra_args lands BEFORE the sidecar flag so callers can pass
        # alternate executables that interpret their own argv (e.g., a
        # Python-impersonator fake in tests: `python fake_sidecar.py
        # --ipc-sidecar` runs the fake which ignores trailing flags).
        # Production: extra_args is empty so this is just
        # `openmarquee-render --ipc-sidecar`.
        args = [self._binary_path, *self._extra_args, "--ipc-sidecar"]
        # Bug 1 follow-up (2026-05-20): hand the sidecar the operator's
        # configured timezone via the TZ env var. The renderer's
        # auto_mode clock calls libc localtime_r, which honors TZ
        # (full IANA zoneinfo + DST). When settings.timezone is unset
        # ("Device local"), TZ is left untouched and the sidecar
        # falls back to the system /etc/localtime. Resolved fresh at
        # each (re)spawn.
        env = dict(os.environ)
        tz = self._get_timezone()
        if tz:
            env["TZ"] = tz
        else:
            # Don't carry a stale TZ inherited from the backend's own
            # env into the sidecar — drop it so localtime_r uses the
            # system zone.
            env.pop("TZ", None)
        log.info(
            "RustRenderer launching subprocess: %s (TZ=%s)",
            " ".join(args),
            env.get("TZ", "<system>"),
        )
        # STREAM/VLC slice 2.5: the dedicated binary frame channel.
        # A pipe whose read end the sidecar inherits (pass_fds) and
        # finds via the OPENMARQUEE_FRAME_FD env var; the write end
        # stays with us for render_frame() to push RGB888 frames.
        frame_read_fd, frame_write_fd = os.pipe()
        env["OPENMARQUEE_FRAME_FD"] = str(frame_read_fd)
        try:
            self._proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # Line-buffered text mode. The Rust side does its own
                # explicit flush after each response; this setting
                # mainly controls our own write side.
                bufsize=1,
                text=True,
                encoding="utf-8",
                env=env,
                # Inherit the frame-pipe read end into the sidecar.
                pass_fds=(frame_read_fd,),
            )
        except FileNotFoundError as e:
            os.close(frame_read_fd)
            os.close(frame_write_fd)
            raise RustRendererSubprocessError(
                f"Rust binary not found at {self._binary_path}: {e}"
            ) from e
        except OSError as e:
            os.close(frame_read_fd)
            os.close(frame_write_fd)
            raise RustRendererSubprocessError(
                f"Failed to launch Rust subprocess: {e}"
            ) from e
        # The sidecar holds the read end now; the parent drops it.
        # Wrap the write end in a BufferedWriter — its flush() writes
        # the whole buffer (looping over partial pipe writes), so
        # render_frame() can hand it a full multi-MB frame.
        os.close(frame_read_fd)
        self._frame_pipe = os.fdopen(frame_write_fd, "wb")
        self._external_frames_active = False
        # Stderr drainer thread. Prevents pipe-buffer-deadlock if the
        # subprocess writes more stderr than ~64 KB before we read.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name="rust-renderer-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in iter(proc.stderr.readline, ""):
                if line:
                    log.info("rust-sidecar stderr: %s", line.rstrip())
        except Exception:
            # Pipe closed on teardown; readline returns "" and the
            # for-loop exits cleanly. Other exceptions (e.g., decode
            # errors on partial UTF-8) are swallowed — we just stop
            # draining; they don't block the main IPC pipe.
            log.debug("stderr drainer exited", exc_info=True)

    # TODO(slice 2): wrap _send_op calls in asyncio.to_thread() (or run
    # via a dedicated executor) when the proxy is consumed from FastAPI
    # request handlers. The blocking readline() will wedge the event loop
    # otherwise.
    def _send_op(
        self,
        op: str,
        params: dict[str, Any] | None,
        *,
        _allow_reconnect: bool = True,
    ) -> dict[str, Any] | None:
        """Serialize one request, read one response, decode it.

        Returns the OpResult body dict (with the `command` tag preserved
        in `__command__` for advance() dispatch) on Ok, or None when the
        response carries `OpResult::Empty`.

        Raises RustRendererSubprocessError on broken pipe / EOF /
        subprocess-already-dead (or RustRendererRespawnedError if the
        proxy auto-reconnected), RustRendererProtocolError on malformed
        responses, RustRendererOpError on `{"err": ...}` responses.

        `_allow_reconnect` is internal — set False when called from the
        reconnect path (re-Open) or close() to avoid recursion.
        """
        with self._lock:
            try:
                return self._send_op_locked(op, params)
            except RustRendererSubprocessError as e:
                if not _allow_reconnect:
                    raise
                respawned = self._attempt_reconnect_locked(reason=str(e))
                if respawned:
                    raise RustRendererRespawnedError(
                        f"subprocess died during op {op!r}; proxy reconnected "
                        f"but session state was lost — caller must replay "
                        f"begin_slide/begin_transition. (cause: {e})"
                    ) from e
                # Reconnect exhausted or disabled: surface the trail.
                trail = "; ".join(self._reconnect_reasons[-self._reconnect_max:]) \
                    if self._reconnect_reasons else "no prior attempts"
                raise RustRendererSubprocessError(
                    f"subprocess died during op {op!r} and reconnect exhausted "
                    f"(max={self._reconnect_max} in {self._reconnect_window_s:.0f}s) "
                    f"-- trail: [{trail}] -- last cause: {e}"
                ) from e

    def _send_op_locked(
        self, op: str, params: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Wire-level send. Caller holds `_lock`. Raises
        RustRendererSubprocessError on any subprocess-layer failure;
        the wrapper `_send_op` decides whether to attempt reconnect."""
        if self._proc is None:
            raise RustRendererError("RustRenderer not opened")
        if self._proc.poll() is not None:
            rc = self._proc.returncode
            raise RustRendererSubprocessError(
                f"Rust subprocess exited (rc={rc}) before op {op!r}"
            )

        request: dict[str, Any] = {"op": op}
        if params is not None:
            request["params"] = params
        line = json.dumps(request, separators=(",", ":"))

        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(line + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise RustRendererSubprocessError(
                f"Failed to write op {op!r}: {e}"
            ) from e

        try:
            assert self._proc.stdout is not None
            response_line = self._proc.stdout.readline()
        except (BrokenPipeError, OSError) as e:
            raise RustRendererSubprocessError(
                f"Failed to read response to op {op!r}: {e}"
            ) from e
        if not response_line:
            # EOF — subprocess closed stdout. Usually means it died.
            rc = self._proc.poll()
            raise RustRendererSubprocessError(
                f"Rust subprocess closed stdout before responding to op "
                f"{op!r} (rc={rc})"
            )

        try:
            resp = json.loads(response_line)
        except json.JSONDecodeError as e:
            raise RustRendererProtocolError(
                f"Invalid JSON response to op {op!r}: {response_line!r} ({e})"
            ) from e

        return self._decode_response(op, resp)

    def _decode_response(
        self, op: str, resp: Any
    ) -> dict[str, Any] | None:
        """Decode the externally-tagged IpcResponse. On Ok, returns the
        OpResult fields dict with the `command` tag preserved as
        `__command__` (so advance() can dispatch). On `command == "empty"`
        returns None. On Err raises RustRendererOpError."""
        if not isinstance(resp, dict):
            raise RustRendererProtocolError(
                f"Top-level response not an object (op {op!r}): {resp!r}"
            )
        if "err" in resp:
            err_body = resp["err"]
            if not isinstance(err_body, dict) or "error" not in err_body:
                raise RustRendererProtocolError(
                    f"Malformed Err response (op {op!r}): {resp!r}"
                )
            raise _classify_op_error(str(err_body["error"]))
        if "ok" not in resp:
            raise RustRendererProtocolError(
                f"Response missing both 'ok' and 'err' (op {op!r}): {resp!r}"
            )
        ok_body = resp["ok"]
        if not isinstance(ok_body, dict) or "result" not in ok_body:
            raise RustRendererProtocolError(
                f"Malformed Ok response (op {op!r}): {resp!r}"
            )
        result = ok_body["result"]
        if not isinstance(result, dict) or "command" not in result:
            raise RustRendererProtocolError(
                f"Ok.result missing 'command' tag (op {op!r}): {resp!r}"
            )
        command = result["command"]
        if command == "empty":
            return None
        body = {k: v for k, v in result.items() if k != "command"}
        body["__command__"] = command
        return body

    @staticmethod
    def _parse_advance_result(body: dict[str, Any] | None) -> AdvanceResult:
        if body is None:
            raise RustRendererProtocolError(
                "advance: got Empty result; expected paint_slide / "
                "paint_transition / slide_complete / idle"
            )
        command = body.get("__command__")
        if command == "paint_slide":
            return PaintSlide(
                slide_id=uuid.UUID(str(body["slide_id"])),
                t_in_slide_ms=int(body["t_in_slide_ms"]),
            )
        if command == "paint_transition":
            return PaintTransition(
                from_id=uuid.UUID(str(body["from"])),
                to=uuid.UUID(str(body["to"])),
                kind=str(body["kind"]),
                progress=float(body["progress"]),
            )
        if command == "slide_complete":
            return SlideComplete(slide_id=uuid.UUID(str(body["slide_id"])))
        if command == "idle":
            return Idle()
        raise RustRendererProtocolError(
            f"advance: unknown command {command!r} (body: {body!r})"
        )

    def _terminate_subprocess(self) -> None:
        proc = self._proc
        if proc is None:
            return
        # STREAM/VLC slice 2.5: close the binary frame channel first.
        # If the sidecar is mid-pump (blocked reading frames), this
        # EOFs that read so it leaves pump-mode and returns to the
        # JSON loop — where the stdin close below then EOFs it out.
        try:
            if self._frame_pipe is not None:
                self._frame_pipe.close()
        except Exception:
            log.debug("frame-pipe close failed during teardown", exc_info=True)
        self._frame_pipe = None
        self._external_frames_active = False
        # Close stdin so the sidecar's outer loop hits EOF and exits
        # cleanly. (The sidecar returns Ok(()) at end-of-stream per
        # `renderer/src/ipc_main.rs::run_ipc_sidecar`.)
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            log.debug("stdin close failed during teardown", exc_info=True)
        try:
            proc.wait(timeout=self.GRACEFUL_EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            log.warning(
                "RustRenderer subprocess didn't exit in %.1fs; terminating",
                self.GRACEFUL_EXIT_TIMEOUT_S,
            )
            proc.terminate()
            try:
                proc.wait(timeout=self.TERMINATE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                log.warning(
                    "RustRenderer subprocess didn't terminate in %.1fs; killing",
                    self.TERMINATE_TIMEOUT_S,
                )
                proc.kill()
                proc.wait()
        if self._stderr_thread is not None and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=1.0)
        self._proc = None
        self._stderr_thread = None

    # ------------------------------------------------------------------
    # Reconnect / watchdog / health.
    # ------------------------------------------------------------------

    def _prune_reconnect_window(self) -> None:
        """Drop reconnect attempt timestamps older than the rolling
        window. Keeps the reason trail aligned with the attempt list."""
        now = time.monotonic()
        cutoff = now - self._reconnect_window_s
        kept = [
            (t, r) for t, r in zip(
                self._reconnect_attempts, self._reconnect_reasons, strict=True
            )
            if t >= cutoff
        ]
        self._reconnect_attempts = [t for t, _ in kept]
        self._reconnect_reasons = [r for _, r in kept]

    def _attempt_reconnect_locked(self, *, reason: str) -> bool:
        """Tear down the (presumed-dead) subprocess and re-launch + re-Open.
        Caller MUST hold `_lock` (or guarantee no other thread accesses
        `self._proc`).

        Returns True if reconnect succeeded; False if disabled
        (reconnect_max_retries=0) or retries exhausted within the
        current rolling window.

        Side effects: appends to `_reconnect_attempts` + `_reconnect_reasons`
        on every attempt (success or fail). On success, leaves a fresh
        subprocess `_opened=True`. On failure, leaves `_proc=None` so the
        next op gets a clean "not opened" error path.
        """
        if self._reconnect_max <= 0:
            log.warning(
                "RustRenderer reconnect disabled (max_retries=0); not respawning"
            )
            return False
        self._prune_reconnect_window()
        if len(self._reconnect_attempts) >= self._reconnect_max:
            log.error(
                "RustRenderer reconnect retries exhausted (%d in %.1fs); not respawning. trail=%s",
                self._reconnect_max,
                self._reconnect_window_s,
                self._reconnect_reasons,
            )
            return False
        # Capture exit code BEFORE teardown so the trail is meaningful.
        rc = None
        if self._proc is not None:
            rc = self._proc.poll()
        attempt_no = len(self._reconnect_attempts) + 1
        full_reason = f"attempt {attempt_no}/{self._reconnect_max} rc={rc}: {reason}"
        self._reconnect_attempts.append(time.monotonic())
        self._reconnect_reasons.append(full_reason)
        log.warning("RustRenderer reconnecting: %s", full_reason)
        try:
            self._terminate_subprocess()
        except Exception:
            log.debug("teardown during reconnect failed", exc_info=True)
        self._opened = False
        try:
            self._launch_subprocess()
            mode_w, mode_h = self._send_open_op()
            # Update negotiated dims in case the panel re-negotiated.
            self.width = mode_w
            self.height = mode_h
            self._opened = True
        except Exception as e:
            log.error("RustRenderer reconnect attempt %d failed: %s", attempt_no, e)
            self._reconnect_reasons[-1] = f"{full_reason} -- reconnect open failed: {e}"
            try:
                self._terminate_subprocess()
            except Exception:
                log.debug("teardown after failed reconnect", exc_info=True)
            return False
        log.info("RustRenderer reconnect succeeded (attempt %d)", attempt_no)
        return True

    def _start_watchdog(self) -> None:
        if not self._watchdog_enabled:
            return
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return  # already running
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="rust-renderer-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

    def _stop_watchdog(self) -> None:
        if self._watchdog_thread is None:
            return
        self._watchdog_stop.set()
        # 2x the interval is enough headroom for one wait() loop to wake.
        # The watchdog never blocks on the main lock (uses non-blocking
        # acquire), so it can't be stuck waiting on a slow op.
        join_timeout = max(2.0, self._watchdog_interval_s * 2.0)
        self._watchdog_thread.join(timeout=join_timeout)
        if self._watchdog_thread.is_alive():
            log.warning(
                "RustRenderer watchdog thread didn't join in %.1fs",
                join_timeout,
            )
        self._watchdog_thread = None

    def _watchdog_loop(self) -> None:
        """Background thread: every `_watchdog_interval_s`, check
        subprocess liveness. On detected death, attempt reconnect under
        the main lock (non-blocking — if an op is in flight, defer to
        the next tick).
        """
        while not self._watchdog_stop.is_set():
            # wait() returns True if the event was set during the wait.
            if self._watchdog_stop.wait(self._watchdog_interval_s):
                return
            proc = self._proc
            if proc is None:
                return
            rc = proc.poll()
            if rc is None:
                continue  # alive
            # Subprocess died. Try to grab the lock NON-BLOCKING; if an
            # op is in flight, that op will detect the death on its own
            # and trigger reconnect there. Don't deadlock here.
            acquired = self._lock.acquire(blocking=False)
            if not acquired:
                log.debug("watchdog saw death but lock held; deferring")
                continue
            try:
                # Re-check inside the lock (race-safe).
                if self._proc is None:
                    return  # close() ran while we were waiting
                if self._proc.poll() is None:
                    continue  # someone reconnected already
                self._attempt_reconnect_locked(
                    reason=f"watchdog: subprocess died (rc={rc})"
                )
            finally:
                self._lock.release()

    def health_probe(self) -> HealthState:
        """On-demand health probe. Returns a `HealthState` snapshot
        derived from `subprocess.poll()` + the proxy's reconnect
        bookkeeping.

        Note: the Rust sidecar does NOT implement a Health op (the
        doc-spec'd periodic Health message in
        `docs/renderer-rewrite-plan-rust.md` §7 hasn't been
        implemented). If/when a server-side Health op lands, extend
        this method to issue it and surface the BackendState value.

        Takes `_lock` so the prune + len + tuple snapshot is atomic
        relative to any concurrent reconnect (watchdog or in-op).
        """
        with self._lock:
            proc = self._proc
            self._prune_reconnect_window()
            if proc is None:
                return HealthState(
                    is_alive=False,
                    exit_code=None,
                    reconnect_attempts_in_window=len(self._reconnect_attempts),
                    reconnect_history=tuple(self._reconnect_reasons),
                )
            rc = proc.poll()
            return HealthState(
                is_alive=(rc is None),
                exit_code=rc,
                reconnect_attempts_in_window=len(self._reconnect_attempts),
                reconnect_history=tuple(self._reconnect_reasons),
            )
