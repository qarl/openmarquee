"""HDMI renderer — writes rendered frames to a Linux framebuffer device.

On the Pi, `/dev/fb0` is the HDMI framebuffer; writes at offset 0 land
pixels on the connected display. On dev boxes without a framebuffer
(including Mac CI), the same code runs against a plain file path so
the byte-layout contract is testable today — we seek(0) + write the
converted frame bytes each tick exactly like the Pi kernel expects,
and tests read the bytes back and assert they're what the connected
HDMI TV would have shown.

Structurally a twin of MockRenderer (same `width`, `height`,
`render_frame(bytes)` protocol), with three additional concerns the
Pi's framebuffer adds:

1. **Pixel format.** The Pi's default HDMI fb is 32-bit BGRA
   (x R G B with ignored alpha). Input is always RGB888 per the
   Renderer protocol; we convert via Pillow's channel split/merge
   trick so the C layer handles the per-pixel loop at speed.
2. **Display upscale.** Signs are small (e.g. 128×96); HDMI displays
   are large (e.g. 1920×1080). The renderer upscales the sign frame
   to `display_width`×`display_height` with `NEAREST` sampling so
   the LED-sign pixel grid stays visible rather than softened by
   bilinear smoothing. Letterboxes with black if aspect ratios
   differ.
3. **Lifecycle.** The underlying fd stays open across render_frame
   calls — reopening /dev/fb0 every tick is wasteful. Context-manager
   protocol closes it cleanly. A bare `close()` is also provided.

Known not-yet-implemented nuances (documented for Phase-6 bring-up):

- **fb line stride.** Some framebuffers pad each row so the row byte
  count isn't `width * bytes_per_pixel`. Querying this needs the
  `FBIOGET_FSCREENINFO` ioctl. Today we assume no stride padding —
  will blow up visibly if a Pi's fb uses padded rows; fix is to
  ioctl-query at __enter__ time.
- **Resolution detection.** `FBIOGET_VSCREENINFO` reports the real
  display_width/display_height + bits-per-pixel; we take them as
  constructor args instead. Phase-6 can default from the ioctl if
  args are omitted.
- **Double-buffering / vsync.** Plain write() to fb0 doesn't page-
  flip, so we can in theory tear. For a slide-cycler at 1Hz the
  naked-eye impact is zero; revisit if we ship auto-slide seconds
  on HDMI.
"""

from __future__ import annotations

import logging
import os
import warnings
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

# Pixel formats we know how to serialize RGB888 into.
#
# - `bgra32` (4 bytes/pixel): the historical "Pi HDMI default" the
#   renderer was built against. Some Pi configurations land in this
#   format (especially after a `framebuffer_depth=32` config.txt
#   knob or with vc4-fkms-v3d).
# - `rgb565` (2 bytes/pixel): what the dev Pi (Bookworm + vc4-kms-v3d
#   compat shim) actually exposes — verified 2026-05-01:
#   `cat /sys/class/graphics/fb0/bits_per_pixel` → `16`. 5 bits R + 6
#   bits G + 5 bits B packed little-endian per pixel.
# - `rgb24` (3 bytes/pixel): convenience for tests where 3-bytes-per-
#   pixel layouts are easier to eyeball than the swizzled paths.
_PIXEL_FORMATS: dict[str, int] = {
    "bgra32": 4,
    "rgb565": 2,
    "rgb24": 3,
}


class HDMIRenderer:
    """Render RGB888 frames to a Linux framebuffer path.

    Args:
        width, height: Sign resolution — what the playback engine
            emits. The Renderer protocol contract.
        display_width, display_height: HDMI display resolution — where
            the bytes actually land. Defaults to the sign resolution
            (no upscale, pure passthrough), which is what tests use.
        output_path: Framebuffer device path. Defaults to /dev/fb0;
            overridden to a tmp file in tests.
        pixel_format: "bgra32" (Pi HDMI default) or "rgb24" (simpler
            byte layout for tests + small custom displays).
    """

    def __init__(
        self,
        width: int,
        height: int,
        *,
        display_width: int | None = None,
        display_height: int | None = None,
        output_path: Path = Path("/dev/fb0"),
        pixel_format: str = "bgra32",
    ):
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive")
        if pixel_format not in _PIXEL_FORMATS:
            raise ValueError(
                f"unsupported pixel_format {pixel_format!r}; "
                f"expected one of {sorted(_PIXEL_FORMATS)}"
            )
        self.width = width
        self.height = height
        self.display_width = display_width if display_width is not None else width
        self.display_height = display_height if display_height is not None else height
        if self.display_width <= 0 or self.display_height <= 0:
            raise ValueError("display_width and display_height must be positive")
        self.output_path = Path(output_path)
        self.pixel_format = pixel_format
        self._bytes_per_pixel = _PIXEL_FORMATS[pixel_format]
        self._fd: int | None = None
        self._frame_bytes = self.display_width * self.display_height * self._bytes_per_pixel

    # --- lifecycle ---

    def __enter__(self) -> HDMIRenderer:
        self._open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def _open(self) -> None:
        if self._fd is not None:
            return
        # O_WRONLY | O_CREAT works on both /dev/fb0 (writes in place)
        # and a fresh tmp file (creates it empty). On the Pi the fb
        # device node already exists — O_CREAT is a no-op there.
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
                log.exception("HDMIRenderer: close failed for %s", self.output_path)
            finally:
                self._fd = None

    # --- render path ---

    def render_frame(self, frame: bytes) -> None:
        """Convert RGB888 `frame` to the fb's native format + write it.

        `frame` is `width * height * 3` bytes, RGB888, top-left first
        (per the Renderer protocol). We resize to display dims using
        NEAREST (pixel-accurate for small LED sign graphics), convert
        to the target pixel format, and write the full display-sized
        payload at offset 0 of the fd.
        """
        expected = self.width * self.height * 3
        if len(frame) != expected:
            raise ValueError(
                f"frame length {len(frame)} does not match "
                f"{self.width}x{self.height} RGB888 (expected {expected} bytes)"
            )

        payload = self._frame_to_display_bytes(frame)
        if len(payload) != self._frame_bytes:
            # Belt-and-braces — a mismatch here points at a conversion
            # bug, not an operator error, so raise not HTTP-return.
            raise AssertionError(
                f"HDMIRenderer: converted frame is {len(payload)} bytes; "
                f"expected {self._frame_bytes}"
            )

        self._open()
        assert self._fd is not None
        os.lseek(self._fd, 0, os.SEEK_SET)
        # write() on /dev/fb0 returns the bytes written; on short writes
        # loop until done. On a tmp file the first call always consumes
        # the whole buffer.
        view = memoryview(payload)
        total = 0
        while total < len(view):
            n = os.write(self._fd, view[total:])
            if n <= 0:
                raise OSError(f"HDMIRenderer: short write to {self.output_path}")
            total += n

    # --- internals ---

    def _frame_to_display_bytes(self, frame: bytes) -> bytes:
        """RGB888 sign frame → native-format display frame bytes."""
        image = Image.frombytes("RGB", (self.width, self.height), frame)

        # Upscale + letterbox only if dims differ; otherwise pass the
        # image through untouched to save a copy on the hot path.
        if (self.width, self.height) != (self.display_width, self.display_height):
            image = self._scale_with_letterbox(image)

        if self.pixel_format == "rgb24":
            return image.tobytes()

        if self.pixel_format == "rgb565":
            # RGB565 little-endian: pack each RGB888 pixel into 2 bytes
            # as `RRRRRGGG GGGBBBBB` viewed MSB-first (5/6/5), stored
            # low-byte-first per the Linux fb little-endian convention.
            #
            # Try Pillow's "BGR;16" mode first — it's the C-implemented
            # path and 7× faster than the numpy fallback on the dev Pi
            # (30ms vs 210ms at 1920×1080, measured 2026-05-01 against
            # Pillow 11.1). Mode is deprecated and slated for removal in
            # Pillow 12 (2025-10-15); fall back to numpy on import error
            # / mode-unsupported so the renderer keeps working when
            # Pillow eventually drops it. Output is byte-identical
            # between paths — verified live on the Pi.
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    return image.convert("BGR;16").tobytes()
            except (ValueError, OSError):
                arr = np.frombuffer(image.tobytes(), dtype=np.uint8).reshape(
                    image.height, image.width, 3
                ).astype(np.uint16, copy=False)
                packed = (
                    ((arr[..., 0] & 0xF8) << 8)
                    | ((arr[..., 1] & 0xFC) << 3)
                    | (arr[..., 2] >> 3)
                )
                return packed.astype("<u2").tobytes()

        # bgra32: swap R and B channels via Pillow's split/merge — the
        # C-implemented inner loop keeps this fast enough for 1080p at
        # 30Hz. Alpha is pinned opaque because most Pi HDMI fbs treat
        # the A byte as "don't care" anyway.
        r, g, b = image.split()
        alpha = Image.new("L", image.size, 255)
        bgra = Image.merge("RGBA", (b, g, r, alpha))
        return bgra.tobytes()

    def _scale_with_letterbox(self, image: Image.Image) -> Image.Image:
        """Upscale `image` to (display_width, display_height), preserving
        aspect ratio, with black letterbox bars filling any mismatch.

        NEAREST sampling keeps pixel-art signs crisp on a big TV — a
        128×96 slide should look like 128×96 stretched, not like someone
        ran it through a gaussian.
        """
        scale = min(
            self.display_width / self.width,
            self.display_height / self.height,
        )
        new_w = max(1, int(round(self.width * scale)))
        new_h = max(1, int(round(self.height * scale)))
        scaled = image.resize((new_w, new_h), resample=Image.Resampling.NEAREST)

        canvas = Image.new(
            "RGB",
            (self.display_width, self.display_height),
            (0, 0, 0),
        )
        off_x = (self.display_width - new_w) // 2
        off_y = (self.display_height - new_h) // 2
        canvas.paste(scaled, (off_x, off_y))
        return canvas
