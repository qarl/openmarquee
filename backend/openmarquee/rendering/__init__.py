"""Renderers — how frames get from the playback engine to a physical (or virtual) display.

One interface, five implementations. All five exist in the tree today;
the three hardware paths (HDMI, WS2812B, HUB75) have fully-tested
byte-prep + config-validation + lifecycle code that runs on a Mac,
with the actual hardware write gated behind a Pi-day switch (either
a tmp-file sink for tests or a NotImplementedError for HUB75's
library path).

- `MockRenderer` — writes frames to a PNG file. Used by
  `scripts/dev.sh` for the live-preview page.
- `HDMIRenderer` — writes BGRA32 bytes to `/dev/fb0` (Pi HDMI
  framebuffer) or a tmp file. NEAREST upscale + letterbox keeps
  small LED-sign pixel-art crisp on a 1920×1080 TV.
- `CompositeRenderer` — HDMIRenderer subclass with NTSC / PAL
  default dims. Requires `/boot/config.txt` tweaks on Pi-day; the
  renderer code path is identical to HDMI.
- `WS2812BRenderer` — encodes RGB888 frames as the GRB chain byte
  stream a WS2812B strip consumes. Configurable pixel map
  (row_major / serpentine / custom) handles strip-built-matrix
  wirings. Real hardware uses rpi_ws281x on Pi-day.
- `HUB75Renderer` — stub. Applies the gamma / brightness LUT and
  validates every hzeller config param today; the panel-write
  path raises NotImplementedError until Phase-8 bring-up subclasses
  `_write_to_panel` with the rgb-led-matrix C library calls.

Pixel-format contract (for every renderer, not just Mock): the playback engine
emits **RGB888 row-major, top-left pixel first**. Renderers are responsible for
any channel swizzle their hardware needs (HDMI → BGRA32, WS2812B → GRB24).
Don't push that swizzle up into the engine.

Lifecycle contract: all hardware renderers support context-manager
(`with HDMIRenderer(...) as r: r.render_frame(bytes)`) and a bare
`close()` for manual cleanup. `render_frame` auto-opens the underlying
sink lazily so ad-hoc usage doesn't require ritual.
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
