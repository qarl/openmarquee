"""Renderers — how frames get from the playback engine to a physical (or virtual) display.

One interface, five implementations:

- `HUB75Renderer` — drives an LED matrix panel via hzeller/rpi-rgb-led-matrix (Pi only).
- `HDMIRenderer` — writes frames to /dev/fb0, or hands MP4 files to ffmpeg.
- `WS2812BRenderer` — pushes pixel data to an addressable LED strip via rpi_ws281x (Pi only).
- `CompositeRenderer` — HDMIRenderer with /boot/config.txt configured for composite out.
- `MockRenderer` — writes frames to a PNG file for dev-time preview.

Only `MockRenderer` exists today. The real renderers land in Phases 6, 8, and 10.

Protocol gaps to address when the first hardware renderer lands (Phase 6 / HDMI):

- **Lifecycle.** HUB75 (GPIO/DMA init), WS2812B (GPIO setup), and HDMI (ffmpeg
  subprocess or framebuffer open) all need `start()` / `stop()` hooks. Likely to
  become `__enter__` / `__exit__` so `with renderer:` is the one obvious way.
- **Frame timing contract.** Does `render_frame` block until the frame is on
  display (vsync), or is it fire-and-forget? Matters for how the playback engine
  paces itself.
- **Brightness / per-renderer config.** HUB75 scan-rate, panel layout, HDMI
  refresh rate, WS2812B pixel remapping. Not in the abstract protocol — they
  live on the concrete class — but the config shape needs to be defined.

Pixel-format contract (for every renderer, not just Mock): the playback engine
emits **RGB888 row-major, top-left pixel first**. Renderers are responsible for
any channel swizzle their hardware needs (HUB75 panels vary; WS2812B strips are
commonly GRB). Don't push that swizzle up into the engine.
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
        first, channel order R, G, B. Implementations that need a different
        channel order (BGR for some HUB75 panels, GRB for WS2812B) must swizzle
        internally.
        """
        ...
