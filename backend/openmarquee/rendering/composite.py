"""Composite-video renderer — thin subclass of HDMIRenderer.

On the Pi, composite TV output is physically routed out the 3.5mm
TRRS jack instead of the HDMI port, but the *rendering* path is
identical: frames are written to the same Linux framebuffer device
(`/dev/fb0`), and the kernel driver + downstream hardware encodes
the NTSC or PAL composite signal. The switch between HDMI and
composite happens in `/boot/config.txt` via:

    # For NTSC 480i composite
    dtoverlay=vc4-kms-v3d,composite=1
    hdmi_ignore_hotplug=1
    sdtv_mode=0     # 0 = NTSC, 2 = PAL, 16 = NTSC progressive, 18 = PAL progressive
    sdtv_aspect=1   # 1 = 4:3, 2 = 14:9, 3 = 16:9

and a reboot. None of that is renderer-code — it's device
provisioning, and lives in `code/system/` config fragments
(Phase 7 / 10 work).

What IS code is the display-dimension default: composite NTSC gives
720×480 (4:3) effective raster; PAL is 720×576. The operator picks
one via the constructor; the default is NTSC because that's what
ships in a US demo setup. Everything else (BGRA32 pixel format,
letterbox upscale, seek-0 write semantics, context-manager
lifecycle) is inherited wholesale from HDMIRenderer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from openmarquee.rendering.hdmi import HDMIRenderer


# Well-known composite modes → (display_width, display_height). These
# are the raster sizes the Pi presents to the framebuffer when the
# sdtv_mode lines in /boot/config.txt are set as documented in the
# module docstring above.
_MODE_DIMS: dict[str, tuple[int, int]] = {
    "ntsc": (720, 480),
    "pal": (720, 576),
}


class CompositeRenderer(HDMIRenderer):
    """HDMIRenderer-compatible renderer pointed at a composite-configured fb.

    Args mirror HDMIRenderer; the one addition is `tv_mode`, which
    picks sensible `display_width` / `display_height` defaults when
    the operator doesn't specify them. Explicit dims always win so a
    non-standard encoder (RF modulator etc.) can still drive an
    arbitrary raster.

    The pixel format stays bgra32: on a correctly-configured Pi the
    fb is still 32bpp regardless of whether the physical output is
    HDMI or composite — only the downstream encoder chain differs.
    """

    def __init__(
        self,
        width: int,
        height: int,
        *,
        tv_mode: Literal["ntsc", "pal"] = "ntsc",
        display_width: int | None = None,
        display_height: int | None = None,
        output_path: Path = Path("/dev/fb0"),
        pixel_format: str = "bgra32",
    ):
        if tv_mode not in _MODE_DIMS:
            raise ValueError(
                f"unknown tv_mode {tv_mode!r}; expected one of {sorted(_MODE_DIMS)}"
            )
        mode_w, mode_h = _MODE_DIMS[tv_mode]
        super().__init__(
            width=width,
            height=height,
            display_width=display_width if display_width is not None else mode_w,
            display_height=display_height if display_height is not None else mode_h,
            output_path=output_path,
            pixel_format=pixel_format,
        )
        self.tv_mode = tv_mode
