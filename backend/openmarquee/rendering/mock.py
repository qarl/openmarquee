"""Dev-time renderer that writes frames to a PNG file.

Used by the `scripts/dev.sh` live-preview page so the UI developer can see what
the eventual real signs would be displaying, without any hardware.
"""

from pathlib import Path

from PIL import Image


class MockRenderer:
    """Renders each frame as a PNG at `output_path`, overwriting the previous one.

    Also retains the most recent frame in-memory as `last_frame` for tests and
    for endpoints that want to serve the preview directly.
    """

    def __init__(self, width: int, height: int, output_path: Path):
        self.width = width
        self.height = height
        self.output_path = Path(output_path)
        self.last_frame: bytes | None = None

    def render_frame(self, frame: bytes) -> None:
        expected = self.width * self.height * 3
        if len(frame) != expected:
            raise ValueError(
                f"frame length {len(frame)} does not match "
                f"{self.width}x{self.height} (expected {expected} bytes)"
            )
        self.last_frame = frame
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.frombytes("RGB", (self.width, self.height), frame)
        image.save(self.output_path, "PNG")
