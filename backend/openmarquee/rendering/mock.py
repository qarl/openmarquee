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

from collections.abc import Callable
from pathlib import Path

from PIL import Image


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
        image = Image.frombytes("RGB", (width, height), frame)
        image.save(self.output_path, "PNG")
