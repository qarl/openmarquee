"""Dev-time renderer that writes frames to a PNG file.

Used by the `scripts/dev.sh` live-preview page + the `/simulator.html`
pop-out so the UI developer can see what the eventual real signs
would be displaying, without any hardware.

Dims can be either static (tests pin to a small canvas for speed) or
dynamic (dev + simulator read them from `SettingsStorage` each frame,
so changing `display_width` / `display_height` in the Settings UI
takes effect without a restart). Rotation-aware via `get_dims`: the
caller decides whether portrait rotations swap dims, not this module.
"""

import struct
import zlib
from collections.abc import Callable
from pathlib import Path


def _encode_png_rgb(width: int, height: int, rgb_bytes: bytes) -> bytes:
    """Encode width*height*3 RGB888 bytes to a PNG byte string.

    Pure-stdlib (struct + zlib). Replaces the prior PIL.Image.save
    dependency so this module no longer pulls in PIL — the Rust IPC
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


class MockRenderer:
    """Renders each frame as a PNG at `output_path`, overwriting the previous one.

    Also retains the most recent frame in-memory as `last_frame` for tests and
    for endpoints that want to serve the preview directly.

    Dims can be set two ways:

    - Static: `MockRenderer(width, height, output_path)` — the old API,
      used by tests + any caller that knows the dims up front.
    - Dynamic: `MockRenderer(output_path=..., get_dims=callable)` — the
      callable returns `(width, height)` each access, so settings changes
      flow through on the next frame without restarting the renderer.

    `width` and `height` are read-only properties in both cases. The
    Renderer protocol treats them as attributes, and protocol-attribute
    conformance is satisfied equally by plain attrs or properties.
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
            # Freeze in a lambda so the property read path is the same as
            # the dynamic case — one code path to maintain.
            static_w, static_h = int(width), int(height)  # type: ignore[arg-type]
            get_dims = lambda: (static_w, static_h)  # noqa: E731
        self._get_dims = get_dims
        self.output_path = Path(output_path)
        self.last_frame: bytes | None = None

    @property
    def width(self) -> int:
        return self._get_dims()[0]

    @property
    def height(self) -> int:
        return self._get_dims()[1]

    def render_frame(self, frame: bytes) -> None:
        width, height = self._get_dims()
        expected = width * height * 3
        if len(frame) != expected:
            raise ValueError(
                f"frame length {len(frame)} does not match "
                f"{width}x{height} (expected {expected} bytes)"
            )
        self.last_frame = frame
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_bytes(_encode_png_rgb(width, height, frame))
