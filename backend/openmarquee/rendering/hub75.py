"""HUB75 renderer — drives HUB75-protocol LED matrix panels.

Real hardware requires `hzeller/rpi-rgb-led-matrix` + a Pi with the
Adafruit RGB Matrix Bonnet (or equivalent GPIO wiring) + panel power.
None of that works on a Mac, so the actual panel-write path is a
stub that raises NotImplementedError until Phase-8 bring-up.

What IS dry-land and covered by tests today:

- **Constructor + config validation.** Every param range (brightness
  0-100, gamma 0.5-4.0, pwm_bits 1-11, chain_length ≥ 1, etc.) is
  enforced at construction so Phase-8 doesn't burn an afternoon on
  "why is the panel black" problems that are actually invalid config.

- **Gamma + brightness LUT.** LEDs are linear-response; the human eye
  is not. A 256-entry LUT precomputed from `gamma` + `brightness` gets
  applied to every RGB byte before the frame goes to the panel. The
  math is well-defined and self-contained, so we test it — Phase-8
  code doesn't have to second-guess the color response.

- **Frame prep pipeline.** `_prepare_frame` applies the LUT + any
  future panel-specific remap (chain-layout mapping, gamma tables
  per color channel, etc.) and returns the bytes that would get
  pushed to the panel. Tests + dev environments can route these to
  an optional `output_path` for inspection — the same trick HDMI
  and WS2812B renderers use.

- **Lifecycle.** Context-manager protocol + idempotent close().

When Phase-8 hardware arrives, only `_write_to_panel` has to light
up — most commonly a subclass that instantiates an
`rgbmatrix.RGBMatrix` in `_open_panel` and calls `SetImage` in
`_write_to_panel`. The Renderer protocol (`width`, `height`,
`render_frame(bytes)`) stays unchanged.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


# Valid pixel_mapper strings (pass-through to the hzeller library on
# hardware day; we just sanity-check the shape here). The library
# accepts a superset; this list covers the common arrangements a sign
# operator would realistically wire up. Add new entries with a note
# on what the string means.
_KNOWN_PIXEL_MAPPERS: set[str] = {
    "U-mapper",  # 2 parallel chains folded into a "U"
    "V-mapper",  # same, V-shape
    "Rotate:90",  # rotate output 90°
    "Rotate:180",  # rotate output 180°
    "Rotate:270",  # rotate output 270°
}


class HUB75Renderer:
    """Render RGB888 frames to a HUB75 LED matrix panel (or chain of panels).

    Args:
        width, height: Logical sign resolution. What the playback
            engine emits. The physical panel arrangement (chain_length
            × parallel_chains) may be larger — the library's
            pixel_mapper handles folding the logical image across
            panels on hardware day.
        chain_length: How many panels are daisy-chained in series.
            1 = single panel. A 64×64 single panel with chain_length=2
            reports as 128×64 physically; the library multiplexes.
        parallel_chains: How many independent chains run side-by-side
            (the Adafruit Bonnet supports up to 3).
        brightness: 0-100, percentage. Applied via the gamma LUT —
            setting brightness=50 halves output linearly.
        gamma: Gamma exponent for the brightness LUT. 2.2 is a
            reasonable default; panel-specific calibration can tune.
        pixel_mapper: Optional hzeller pixel_mapper string; validated
            against a known-good allowlist here.
        pwm_bits: 1-11 bits of PWM resolution. Higher = smoother
            color gradations, lower = faster refresh.
        gpio_slowdown: 1-4; compensates for Pi CPU speed vs panel
            timing. Pi Zero 2 W usually wants 2.
        output_path: Optional file path. When set, `render_frame`
            writes the prepared bytes here (dry-land observability).
            When None (Pi-day default), `_write_to_panel` is called
            and — in this stub — raises NotImplementedError.
    """

    def __init__(
        self,
        width: int,
        height: int,
        *,
        chain_length: int = 1,
        parallel_chains: int = 1,
        brightness: int = 100,
        gamma: float = 2.2,
        pixel_mapper: str | None = None,
        pwm_bits: int = 11,
        gpio_slowdown: int = 1,
        output_path: Path | None = None,
    ):
        # Dimensions.
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive")

        # Panel multiplexing counts.
        if chain_length < 1:
            raise ValueError("chain_length must be >= 1")
        if parallel_chains < 1 or parallel_chains > 3:
            raise ValueError("parallel_chains must be in 1..3 (Adafruit Bonnet caps at 3)")

        # Color-response params.
        if not (0 <= brightness <= 100):
            raise ValueError("brightness must be in 0..100")
        if not (0.5 <= gamma <= 4.0):
            raise ValueError("gamma must be in 0.5..4.0 (usable range)")

        # Library knobs.
        if not (1 <= pwm_bits <= 11):
            raise ValueError("pwm_bits must be in 1..11 (hzeller library range)")
        if not (1 <= gpio_slowdown <= 4):
            raise ValueError("gpio_slowdown must be in 1..4")

        if pixel_mapper is not None:
            if not isinstance(pixel_mapper, str) or not pixel_mapper.strip():
                raise ValueError("pixel_mapper must be a non-empty string")
            if pixel_mapper not in _KNOWN_PIXEL_MAPPERS:
                # Warn, don't reject — the library's superset is wider
                # than our allowlist; a typo is more likely than a valid
                # exotic mapper, so log it for operator visibility.
                log.warning(
                    "HUB75Renderer: pixel_mapper=%r not in known-good set "
                    "%s; passing through but confirm it's a real hzeller "
                    "pixel_mapper expression",
                    pixel_mapper,
                    sorted(_KNOWN_PIXEL_MAPPERS),
                )

        self.width = width
        self.height = height
        self.chain_length = chain_length
        self.parallel_chains = parallel_chains
        self.brightness = brightness
        self.gamma = gamma
        self.pixel_mapper = pixel_mapper
        self.pwm_bits = pwm_bits
        self.gpio_slowdown = gpio_slowdown
        self.output_path = Path(output_path) if output_path is not None else None

        self._gamma_lut = _build_gamma_lut(gamma, brightness)
        self._fd: int | None = None

    # --- derived properties ---

    @property
    def physical_width(self) -> int:
        """Total pixel columns across all chained panels in a row."""
        return self.width * self.chain_length

    @property
    def physical_height(self) -> int:
        """Total pixel rows across all parallel chains."""
        return self.height * self.parallel_chains

    # --- lifecycle ---

    def __enter__(self) -> HUB75Renderer:
        self._open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def _open(self) -> None:
        """Open the output sink.

        Stub: opens the optional `output_path` file fd for dry-land
        tests. Hardware-day subclass opens the hzeller RGBMatrix.
        """
        if self._fd is not None or self.output_path is None:
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
                log.exception("HUB75Renderer: close failed for %s", self.output_path)
            finally:
                self._fd = None

    # --- render path ---

    def render_frame(self, frame: bytes) -> None:
        """Prepare `frame` (RGB888 `width * height * 3` bytes) and
        hand it to the panel sink.

        Split into `_prepare_frame` (pure-function, dry-land) +
        `_write_to_panel` (hardware) so the interesting logic is
        covered by tests today and Phase-8 bring-up only has to
        light up the panel write.
        """
        expected = self.width * self.height * 3
        if len(frame) != expected:
            raise ValueError(
                f"frame length {len(frame)} does not match "
                f"{self.width}x{self.height} RGB888 (expected {expected} bytes)"
            )

        prepared = self._prepare_frame(frame)

        if self.output_path is not None:
            # Dry-land path: dump prepared bytes to the fd so tests can
            # read them back. Same seek-0 + full-payload write shape as
            # the other file-backed renderers.
            self._open()
            assert self._fd is not None
            os.lseek(self._fd, 0, os.SEEK_SET)
            view = memoryview(prepared)
            total = 0
            while total < len(view):
                n = os.write(self._fd, view[total:])
                if n <= 0:
                    raise OSError(f"HUB75Renderer: short write to {self.output_path}")
                total += n
        else:
            # Hardware path. Overridden by Phase-8 subclass.
            self._write_to_panel(prepared)

    def _prepare_frame(self, frame: bytes) -> bytes:
        """Apply the gamma/brightness LUT to every RGB byte.

        Future panel-layout remapping (chain spread, parallel stacks,
        per-channel LUTs) lands in this pipeline too. Keeping it a
        pure function from input bytes → output bytes means the test
        suite can lock in each transformation as it lands.
        """
        lut = self._gamma_lut
        out = bytearray(len(frame))
        for i, b in enumerate(frame):
            out[i] = lut[b]
        return bytes(out)

    def _write_to_panel(self, prepared: bytes) -> None:
        """Push prepared bytes to the physical HUB75 panel. STUB.

        Phase-8 bring-up overrides this — most commonly by holding
        an `rgbmatrix.RGBMatrix` instance constructed from
        self.{chain_length, parallel_chains, brightness, pwm_bits,
        gpio_slowdown, pixel_mapper}, wrapping `prepared` in a
        `PIL.Image` of the panel's physical dims, and calling
        `canvas.SetImage(image); matrix.SwapOnVSync(canvas)`.
        """
        raise NotImplementedError(
            "HUB75Renderer requires the hzeller/rpi-rgb-led-matrix "
            "library + real panel hardware; Phase-8 bring-up wires this. "
            "Pass output_path=<tmp file> to exercise the frame-prep path "
            "on dev + CI."
        )


def _build_gamma_lut(gamma: float, brightness: int) -> list[int]:
    """Precompute a 256-entry LUT mapping linear 8-bit input to
    gamma-corrected + brightness-scaled 8-bit output.

    Math: output = round(((input / 255) ** gamma) * (brightness / 100) * 255)
    Clamped to [0, 255]. Monotonic non-decreasing. 0 → 0 always.
    """
    scale = brightness / 100.0
    lut: list[int] = []
    for i in range(256):
        linear = (i / 255.0) ** gamma
        scaled = linear * scale * 255.0
        v = int(round(scaled))
        if v < 0:
            v = 0
        elif v > 255:
            v = 255
        lut.append(v)
    return lut
