"""Dev-time renderer that satisfies the IPC contract.

DELETE-PIL slice 11: MockRenderer is now IPC-shaped. It implements
the same surface as RustRenderer (begin_slide / advance /
begin_transition / capture / open / close / __enter__ / __exit__)
so the playback loop drives it through `_play_via_rust_ipc` instead
of the PIL `_play_dynamic_slide_software` fork. Internally it's
still a placeholder: each `begin_slide` writes a deterministic
solid-color PNG (color derived from a hash of the slide id) to
`output_path` so the dev preview live-updates. No real composition
happens here -- the placeholder is a visibility signal only.

`render_frame(bytes)` is preserved as a backward-compat helper for
tests + AutoFallbackRenderer's fallback path + capture_current_frame.

Used by:
- `scripts/dev.sh` live-preview page (polls `/dev/preview/frame.png`)
- CI tests that need a Renderer without a real DRM stack
- `AutoFallbackRenderer` fallback target when the Rust subprocess
  exhausts its respawn budget

Dims can be either static (tests pin to a small canvas for speed) or
dynamic (dev + simulator read them from `SettingsStorage` each frame,
so changing `display_width` / `display_height` in the Settings UI
takes effect without a restart). Rotation-aware via `get_dims`: the
caller decides whether portrait rotations swap dims, not this module.
"""

import hashlib
import struct
import uuid
import zlib
from collections.abc import Callable
from pathlib import Path

# Import the canonical IPC result dataclasses. `playback.py` does
# `isinstance(result, SlideComplete)` / `isinstance(result, PaintTransition)`
# against the canonical classes, so MockRenderer.advance() MUST return
# instances of these exact classes (not local re-declarations) or the
# isinstance checks return False and the playback loop misroutes. The
# canonical types live near the top of rust_renderer.py before any
# subprocess machinery, so importing them doesn't drag in IPC plumbing.
from openmarquee.rendering.rust_renderer import (
    CaptureResult,
    Idle,
    PaintSlide,
    PaintTransition,
    SlideComplete,
)


def _encode_png_rgb(width: int, height: int, rgb_bytes: bytes) -> bytes:
    """Encode width*height*3 RGB888 bytes to a PNG byte string.

    Pure-stdlib (struct + zlib). Replaces the prior PIL.Image.save
    dependency so this module no longer pulls in PIL -- the Rust IPC
    sidecar owns all real rendering on production; MockRenderer's PNG
    output is just a serialization step for the dev live-preview page.
    """
    def chunk(name: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + name
            + data
            + struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF)
        )

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(
        b"IHDR",
        struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
    )
    row_stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type: None
        raw.extend(rgb_bytes[y * row_stride : (y + 1) * row_stride])
    idat = chunk(b"IDAT", zlib.compress(bytes(raw)))
    iend = chunk(b"IEND", b"")
    return signature + ihdr + idat + iend


def _placeholder_rgb_bytes(width: int, height: int, slide_id: str) -> bytes:
    """Return width*height*3 RGB888 bytes filled with a deterministic
    color derived from `slide_id`. The dev preview live-updates as
    slides change, so different slides show different solid colors --
    enough visual signal to confirm playback is alive without
    composing actual content."""
    if slide_id:
        digest = hashlib.sha256(slide_id.encode("utf-8")).digest()
        r, g, b = digest[0], digest[1], digest[2]
    else:
        r = g = b = 0
    return bytes((r, g, b)) * (width * height)


class MockRenderer:
    """IPC-shaped dev-time renderer. Writes placeholder PNGs.

    Satisfies the Renderer protocol AND the IPC ops contract
    (begin_slide / advance / begin_transition / capture / open /
    close) so the playback loop's `_play_via_rust_ipc` route drives
    it directly -- no PIL fallback needed.

    Dims can be set two ways:

    - Static: `MockRenderer(width, height, output_path)` -- the old
      API, used by tests + any caller that knows the dims up front.
    - Dynamic: `MockRenderer(output_path=..., get_dims=callable)` --
      the callable returns `(width, height)` each access, so settings
      changes flow through on the next frame without restarting the
      renderer.

    `width` and `height` are read-only properties in both cases.
    """

    def __init__(
        self,
        width: int | None = None,
        height: int | None = None,
        output_path: Path | None = None,
        *,
        get_dims: Callable[[], tuple[int, int]] | None = None,
    ):
        if output_path is None:
            raise TypeError("output_path is required")
        if get_dims is not None and (width is not None or height is not None):
            raise ValueError(
                "pass either static width+height OR get_dims, not both"
            )
        if get_dims is None and (width is None or height is None):
            raise ValueError("need both width and height, or a get_dims callable")
        if get_dims is None:
            # Freeze in a lambda so the property read path is the same
            # as the dynamic case -- one code path to maintain.
            static_w, static_h = int(width), int(height)  # type: ignore[arg-type]
            get_dims = lambda: (static_w, static_h)  # noqa: E731
        self._get_dims = get_dims
        self.output_path = Path(output_path)
        self.last_frame: bytes | None = None

        # IPC-op call recorders (tests + AutoFallbackRenderer assert
        # against these to verify the loop drove the renderer correctly).
        self.begin_slide_calls: list[tuple[uuid.UUID | str, int, int]] = []
        self.begin_transition_calls: list[
            tuple[uuid.UUID | str, int, str, int, int]
        ] = []
        self.advance_calls: list[int] = []
        self.render_frame_calls: int = 0
        self.end_external_frames_calls: int = 0
        self.reopen_calls: int = 0
        self.capture_calls: list[str] = []

        # State machine mirror -- same shape as the Rust sidecar's
        # playback.rs::advance returns. None until open + first
        # begin_slide.
        self._current_slide: uuid.UUID | str | None = None
        self._begin_t_ms: int = 0
        self._duration_ms: int = 0
        self._transition_from: uuid.UUID | str | None = None
        self._transition_to: uuid.UUID | str | None = None
        self._transition_kind: str | None = None
        self._transition_t0_ms: int = 0
        self._transition_ms: int = 0
        self._open: bool = False

    @property
    def width(self) -> int:
        return self._get_dims()[0]

    @property
    def height(self) -> int:
        return self._get_dims()[1]

    # ------------------------------------------------------------------
    # Context manager / lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> "MockRenderer":
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def open(self) -> tuple[int, int]:
        """Open the renderer. Returns negotiated (width, height) --
        mirrors RustRenderer.open()'s OpenResult mode tuple shape."""
        self._open = True
        return self._get_dims()

    def close(self) -> None:
        self._open = False

    # ------------------------------------------------------------------
    # IPC ops (mirror RustRenderer's surface)
    # ------------------------------------------------------------------

    def begin_slide(
        self,
        slide_id: uuid.UUID | str,
        t0_ms: int,
        duration_ms: int,
    ) -> None:
        """Op 2. Cache the slide + reset per-slide playback state.
        Paints a deterministic placeholder PNG to `output_path` so
        the dev preview live-updates."""
        self.begin_slide_calls.append((slide_id, int(t0_ms), int(duration_ms)))
        self._current_slide = slide_id
        self._begin_t_ms = int(t0_ms)
        self._duration_ms = int(duration_ms)
        self._paint_placeholder(str(slide_id))

    def advance(self, t_ms: int):
        """Op 3. Return one of PaintSlide / PaintTransition /
        SlideComplete / Idle based on the current state machine.

        Mirrors the Rust sidecar's `playback.rs::advance`: transition
        state takes precedence; once `elapsed >= transition_ms` the
        to-slide promotes and subsequent calls return PaintSlide for
        the new slide."""
        self.advance_calls.append(int(t_ms))
        if self._transition_to is not None:
            elapsed = t_ms - self._transition_t0_ms
            if elapsed >= self._transition_ms:
                self._current_slide = self._transition_to
                self._begin_t_ms = self._transition_t0_ms + self._transition_ms
                self._transition_from = None
                to = self._transition_to
                self._transition_to = None
                self._transition_kind = None
                return PaintSlide(slide_id=to, t_in_slide_ms=0)
            return PaintTransition(
                from_id=self._transition_from,
                to=self._transition_to,
                kind=self._transition_kind or "cut",
                progress=elapsed / max(1, self._transition_ms),
            )
        if self._current_slide is None:
            return Idle()
        elapsed_in_slide = t_ms - self._begin_t_ms
        if elapsed_in_slide >= self._duration_ms:
            return SlideComplete(slide_id=self._current_slide)
        return PaintSlide(
            slide_id=self._current_slide,
            t_in_slide_ms=elapsed_in_slide,
        )

    def begin_transition(
        self,
        to_slide_id: uuid.UUID | str,
        to_duration_ms: int,
        kind: str,
        transition_ms: int,
        t0_ms: int,
    ) -> None:
        """Op 4. Stage a transition into `to_slide_id`. Subsequent
        advance() calls return PaintTransition until the window
        elapses; at that point advance() promotes the to-slide."""
        self.begin_transition_calls.append(
            (to_slide_id, int(to_duration_ms), kind, int(transition_ms), int(t0_ms))
        )
        self._transition_from = self._current_slide
        self._transition_to = to_slide_id
        self._transition_kind = kind
        self._transition_t0_ms = int(t0_ms)
        self._transition_ms = int(transition_ms)
        self._duration_ms = int(to_duration_ms)
        # Paint a placeholder so dev preview reflects the transition
        # starting (color shifts to the to-slide's color even though
        # the blend itself isn't actually being rendered).
        self._paint_placeholder(str(to_slide_id))

    def capture(self, path: str | Path) -> CaptureResult:
        """Op 5. Write the current placeholder PNG to `path`.
        Mirrors RustRenderer.capture's CaptureResult shape."""
        target = Path(path)
        self.capture_calls.append(str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        slide_id = str(self._current_slide) if self._current_slide else ""
        width, height = self._get_dims()
        png_bytes = _encode_png_rgb(
            width, height, _placeholder_rgb_bytes(width, height, slide_id)
        )
        target.write_bytes(png_bytes)
        return CaptureResult(path=str(target), bytes=len(png_bytes))

    # ------------------------------------------------------------------
    # Backward-compat push-frame surface (tests, AutoFallback fallback)
    # ------------------------------------------------------------------

    def render_frame(self, frame: bytes) -> None:
        """Accept a raw RGB888 frame, cache it, write a PNG to
        output_path. Preserved for tests + AutoFallbackRenderer's
        fallback path + capture_current_frame's pull path."""
        width, height = self._get_dims()
        expected = width * height * 3
        if len(frame) != expected:
            raise ValueError(
                f"frame length {len(frame)} does not match "
                f"{width}x{height} (expected {expected} bytes)"
            )
        self.render_frame_calls += 1
        self.last_frame = frame
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_bytes(_encode_png_rgb(width, height, frame))

    def end_external_frames(self) -> None:
        """No-op for the mock — render_frame() paints in-process, so
        there is no out-of-process pump session to close. Present so
        MockRenderer satisfies the Renderer protocol and the VLC
        pumps can call it unconditionally. Counted for tests."""
        self.end_external_frames_calls += 1

    def reopen(self) -> None:
        """No-op for the mock — there is no out-of-process subprocess
        whose Open-time config (display rotation, FYS bug 5) needs a
        restart. Present so MockRenderer satisfies the Renderer
        protocol. Counted for tests."""
        self.reopen_calls += 1

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _paint_placeholder(self, slide_id: str) -> None:
        """Write a deterministic solid-color PNG keyed on `slide_id`
        to `output_path`. Updates `last_frame` so callers that pull
        the cached bytes see the placeholder."""
        width, height = self._get_dims()
        rgb = _placeholder_rgb_bytes(width, height, slide_id)
        self.last_frame = rgb
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_bytes(_encode_png_rgb(width, height, rgb))
