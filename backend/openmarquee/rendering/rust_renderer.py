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

## Failure model (slice 1: fail loud, no auto-respawn)

- Subprocess fails to launch: `RustRendererSubprocessError`.
- Subprocess dies mid-session: next op call raises
  `RustRendererSubprocessError`. No silent respawn — that's a later
  robustness slice.
- Sidecar returns `{"err": {"error": "..."}}`: `RustRendererOpError`
  with `.message` = the wire-format error string (byte-stable per
  `renderer/src/ipc_main.rs` cargo tests at commit 601820f). Match
  against `.message` for error-class dispatch.
- Malformed JSON or unknown variant: `RustRendererProtocolError`.

## Liveness

The doc-spec'd Health periodic message doesn't exist in the actual
sidecar; use `is_alive` (subprocess.poll() based) as the liveness signal.
A future robustness slice can add an active health probe op if needed.

## Thread safety

IPC is request-response synchronous; `_send_op` is guarded by an
internal lock so concurrent callers serialize cleanly. The stderr reader
thread runs in the background and drains the subprocess's stderr pipe to
prevent deadlock when the subprocess logs more than the pipe-buffer
worth of stderr.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import uuid
from dataclasses import dataclass
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


class RustRendererProtocolError(RustRendererError):
    """The wire protocol was violated: invalid JSON, missing required
    fields, unknown response variant. Indicates a doc / impl drift or
    a corrupted pipe.
    """


class RustRendererOpError(RustRendererError):
    """The sidecar returned `{"err": {"error": "..."}}` to an op.

    `.message` is the verbatim error string from the sidecar. These
    strings are byte-stable per the cargo tests in
    `renderer/src/ipc_main.rs` (commit 601820f). Match against them
    for error-class dispatch:

      - "paint_slide: image_slide requires content_root (--content-root)"
      - "paint_slide: video slides TBD (image + text both supported)"
      - "paint_transition: from non-text slide TBD"
      - "paint_transition: to non-text slide TBD"
      - "Capture: video slides TBD (image + text both supported)"
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


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
    ):
        # Renderer Protocol attrs. Refreshed from the sidecar's
        # OpenOk response after open() so the negotiated mode wins
        # if the panel sets a different res than requested.
        self.width = int(width)
        self.height = int(height)

        self._binary_path = str(
            binary_path
            or os.environ.get("OPENMARQUEE_RUST_BINARY", self.DEFAULT_BINARY)
        )
        self._content_root = str(content_root) if content_root is not None else None
        self._drm_card = drm_card
        self._output = output
        self._extra_args = list(extra_args or [])

        self._proc: subprocess.Popen[str] | None = None
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._opened = False

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
            params: dict[str, Any] = {"output": self._output}
            if self._drm_card is not None:
                params["drm_card"] = self._drm_card
            if self._content_root is not None:
                params["content_root"] = self._content_root
            body = self._send_op("open", params)
            if body is None or "mode_w" not in body or "mode_h" not in body:
                raise RustRendererProtocolError(
                    f"Open OK response missing mode_w/mode_h: {body!r}"
                )
            mode_w = int(body["mode_w"])
            mode_h = int(body["mode_h"])
            self.width = mode_w
            self.height = mode_h
            self._opened = True
            return OpenResult(mode_w=mode_w, mode_h=mode_h)
        except Exception:
            # Open failed; tear down so the next open() can re-launch
            # without confusion.
            self._terminate_subprocess()
            raise

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
        if self._proc is None:
            return
        try:
            if self._opened:
                try:
                    self._send_op("close", None)
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
        """The Rust sidecar owns GPU-side composition; frames never cross the
        process boundary. Use the IPC ops (begin_slide / advance / capture)
        instead. Slice 4 will teach playback.py to bypass render_frame for
        RustRenderer instances.
        """
        raise NotImplementedError(
            "RustRenderer doesn't accept push-frame rendering; use begin_slide/"
            "advance/capture IPC ops. Slice 4 will wire playback.py's bypass."
        )

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
        log.info("RustRenderer launching subprocess: %s", " ".join(args))
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
            )
        except FileNotFoundError as e:
            raise RustRendererSubprocessError(
                f"Rust binary not found at {self._binary_path}: {e}"
            ) from e
        except OSError as e:
            raise RustRendererSubprocessError(
                f"Failed to launch Rust subprocess: {e}"
            ) from e
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
                    log.debug("rust-sidecar stderr: %s", line.rstrip())
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
    def _send_op(self, op: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
        """Serialize one request, read one response, decode it.

        Returns the OpResult body dict (with the `command` tag preserved
        in `__command__` for advance() dispatch) on Ok, or None when the
        response carries `OpResult::Empty`.

        Raises RustRendererSubprocessError on broken pipe / EOF /
        subprocess-already-dead, RustRendererProtocolError on malformed
        responses, RustRendererOpError on `{"err": ...}` responses.
        """
        with self._lock:
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
            raise RustRendererOpError(str(err_body["error"]))
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
