"""Renderers — how frames get from the playback engine to a physical (or virtual) display.

The active production path is the Rust IPC sidecar (`rust_renderer.py`):
the playback loop drives `openmarquee-render --ipc-sidecar` as a
subprocess, sends typed ops over stdin/stdout JSON-lines, and the
sidecar paints frames via EGL + DRM/KMS on the Pi.

- `RustRenderer` — IPC proxy. The production renderer.
- `MockRenderer` — IPC-shaped stub for tests + the dev-host fallback
  branch of `AutoFallbackRenderer`. No pixels are painted in Python.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class Renderer(Protocol):
    """Common interface all renderers conform to.

    Implementations may expose richer methods (start/stop, brightness, etc.) but
    every renderer must at minimum be able to accept a raw RGB frame of known
    dimensions.
    """

    width: int
    height: int

    def render_frame(self, frame: bytes) -> None:
        """Render one RGB frame.

        `frame` is row-major RGB888: `width * height * 3` bytes, top-left pixel
        first, channel order R, G, B.
        """
        ...
