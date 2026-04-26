"""WS2812B renderer — encodes RGB888 frames as the GRB wire stream a
daisy-chained WS2812B strip consumes.

WS2812B LEDs are daisy-chained: the controller streams a continuous
bitstream to LED 0, which consumes its 24 bits (in **GRB** order —
Green, Red, Blue — not RGB!) and forwards the rest down the chain.
Real hardware drives this via the `rpi_ws281x` C library's DMA/PWM
path. For dev + CI we don't have a strip — instead we write the
encoded GRB chain bytes to a plain file path so the encoder's byte
layout is testable today and the Phase-10 bring-up only needs to
swap the output sink.

Two things the encoder has to get right that a naive
"concatenate RGB rows" would flunk:

1. **Channel order.** On the wire, each LED is G then R then B. An
   RGB888 source triple `(10, 20, 30)` must serialise as bytes
   `(20, 10, 30)` on the chain.
2. **Pixel map.** The physical order of LEDs in an LED *matrix*
   built from a strip rarely matches raster order. Common
   arrangements:

   - `row_major`: LED 0 at (0, 0), scan left→right then top→bottom.
   - `serpentine`: row 0 runs L→R, row 1 runs R→L, row 2 L→R, … —
     how most strip-built matrices are actually wired (saves a
     jumper wire per row).
   - custom `list[tuple[int, int]]`: LED i ↔ explicit (x, y).

Lifecycle mirrors `HDMIRenderer` — context-manager preferred, bare
`close()` for manual cleanup, `render_frame` auto-opens the fd so
tests don't have to ritualise.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


# Typed label → builder for the built-in pixel-map strategies. Kept as
# a dict rather than enum so adding a new strategy is one import-safe line.
def _row_major(width: int, height: int) -> list[tuple[int, int]]:
    return [(x, y) for y in range(height) for x in range(width)]


def _serpentine(width: int, height: int) -> list[tuple[int, int]]:
    pixels: list[tuple[int, int]] = []
    for y in range(height):
        xs = range(width) if y % 2 == 0 else range(width - 1, -1, -1)
        for x in xs:
            pixels.append((x, y))
    return pixels


_BUILTIN_STRATEGIES = {
    "row_major": _row_major,
    "serpentine": _serpentine,
}


class WS2812BRenderer:
    """Render RGB888 frames to a GRB byte stream for a WS2812B chain.

    Args:
        width, height: Source-frame resolution — what the playback
            engine emits (RGB888). Must match the dimensions of the
            logical image you want to map onto the LED chain.
        pixel_map: Wiring strategy. One of:

            - `"row_major"` — LED i corresponds to `(i % width, i // width)`.
            - `"serpentine"` — rows alternate direction.
            - `list[tuple[int, int]]` — explicit per-LED (x, y) map;
              length defines the chain length, each (x, y) must lie
              inside `width × height`.

        output_path: Where the encoded chain bytes land. Defaults to
            a /tmp file for local dev — override to a distinct path
            per-renderer in tests so parallel runs don't collide.
            Phase-10 bring-up swaps the sink to `rpi_ws281x` DMA.
    """

    def __init__(
        self,
        width: int,
        height: int,
        *,
        pixel_map: str | list[tuple[int, int]] = "row_major",
        output_path: Path = Path("/tmp/openmarquee-ws2812b.bin"),
    ):
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive")
        self.width = width
        self.height = height
        self._pixel_map = self._resolve_pixel_map(pixel_map, width, height)
        self.led_count = len(self._pixel_map)
        self.output_path = Path(output_path)
        self._fd: int | None = None

    # --- lifecycle ---

    def __enter__(self) -> WS2812BRenderer:
        self._open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def _open(self) -> None:
        if self._fd is not None:
            return
        self._fd = os.open(
            self.output_path,
            os.O_WRONLY | os.O_CREAT,
            0o644,
        )

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                log.exception("WS2812BRenderer: close failed for %s", self.output_path)
            finally:
                self._fd = None

    # --- render path ---

    def render_frame(self, frame: bytes) -> None:
        """Encode `frame` (RGB888 raster) and write the GRB chain bytes.

        Per the Renderer protocol, `frame` is `width * height * 3`
        bytes, top-left pixel first, R G B per pixel. The output is
        `led_count * 3` bytes, chain-order, G R B per LED.
        """
        expected = self.width * self.height * 3
        if len(frame) != expected:
            raise ValueError(
                f"frame length {len(frame)} does not match "
                f"{self.width}x{self.height} RGB888 (expected {expected} bytes)"
            )

        payload = self._encode_chain(frame)

        self._open()
        assert self._fd is not None
        os.lseek(self._fd, 0, os.SEEK_SET)
        view = memoryview(payload)
        total = 0
        while total < len(view):
            n = os.write(self._fd, view[total:])
            if n <= 0:
                raise OSError(f"WS2812BRenderer: short write to {self.output_path}")
            total += n

    # --- internals ---

    def _encode_chain(self, frame: bytes) -> bytes:
        """RGB888 raster → GRB bytes in pixel-map order."""
        out = bytearray(self.led_count * 3)
        row_stride = self.width * 3
        for led_idx, (x, y) in enumerate(self._pixel_map):
            src = y * row_stride + x * 3
            r = frame[src]
            g = frame[src + 1]
            b = frame[src + 2]
            out[led_idx * 3] = g
            out[led_idx * 3 + 1] = r
            out[led_idx * 3 + 2] = b
        return bytes(out)

    @staticmethod
    def _resolve_pixel_map(
        strategy: str | list[tuple[int, int]],
        width: int,
        height: int,
    ) -> list[tuple[int, int]]:
        if isinstance(strategy, str):
            builder = _BUILTIN_STRATEGIES.get(strategy)
            if builder is None:
                raise ValueError(
                    f"unknown pixel_map strategy {strategy!r}; "
                    f"expected one of {sorted(_BUILTIN_STRATEGIES)} "
                    "or a list[tuple[int, int]]"
                )
            return builder(width, height)
        if not isinstance(strategy, list):
            raise TypeError(
                f"pixel_map must be a strategy name or list[tuple[int, int]]; "
                f"got {type(strategy).__name__}"
            )
        if len(strategy) == 0:
            raise ValueError("custom pixel_map must be non-empty")
        resolved: list[tuple[int, int]] = []
        for i, entry in enumerate(strategy):
            if (
                not isinstance(entry, tuple)
                or len(entry) != 2
                or not all(isinstance(c, int) for c in entry)
            ):
                raise TypeError(f"pixel_map[{i}] must be a (x, y) tuple of ints; got {entry!r}")
            x, y = entry
            if not (0 <= x < width and 0 <= y < height):
                raise ValueError(
                    f"pixel_map[{i}] = ({x}, {y}) is outside the {width}x{height} frame"
                )
            resolved.append((x, y))
        return resolved
