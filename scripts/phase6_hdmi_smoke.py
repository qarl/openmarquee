"""Phase 6 HDMI bring-up smoke test — pixels-on-glass live fire.

Renders one TextSlide ("HELLO PHASE 6") at the sign's small native
resolution and pushes it through HDMIRenderer to the Pi's framebuffer.
Run on the dev Pi (openmarquee-dev.local) with the HDMI monitor
connected. Exit criteria: text visibly on screen.

Usage (on the Pi):

    cd /home/openmarquee/openmarquee/backend
    source .venv/bin/activate
    python ../scripts/phase6_hdmi_smoke.py

The fb geometry is auto-detected from /sys/class/graphics/fb0:

    bits_per_pixel  → pixel_format (16 → rgb565, 32 → bgra32)
    virtual_size    → display_width, display_height

Sign-side rendering is fixed at 128×96 (a typical small LED-sign
panel) so the NEAREST upscale path actually exercises — a no-op
1080p-to-1080p pass-through wouldn't prove much. Per §5.10a the
font is sized to box width by default.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

# Allow running from the scripts dir without installing the package.
ROOT = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from openmarquee.rendering.hdmi import HDMIRenderer  # noqa: E402
from openmarquee.seed import render_text_slide_png  # noqa: E402

SIGN_W = 128
SIGN_H = 96
TEXT = "HELLO PHASE 6"


def _detect_fb() -> tuple[int, int, str]:
    """Read /sys/class/graphics/fb0 to get display dims + pick pixel format.

    Returns (display_width, display_height, pixel_format).
    Raises FileNotFoundError if /dev/fb0 is missing — caller prints
    a helpful message + exits non-zero.
    """
    sys_root = Path("/sys/class/graphics/fb0")
    virtual = (sys_root / "virtual_size").read_text().strip()
    bpp = int((sys_root / "bits_per_pixel").read_text().strip())
    w_s, h_s = virtual.split(",")
    width, height = int(w_s), int(h_s)
    if bpp == 16:
        fmt = "rgb565"
    elif bpp == 32:
        fmt = "bgra32"
    else:
        raise ValueError(f"unsupported fb bpp {bpp}")
    return width, height, fmt


def main() -> int:
    fb = Path("/dev/fb0")
    if not fb.exists():
        print(f"ERR: {fb} missing — is the HDMI monitor connected?", file=sys.stderr)
        return 1

    display_w, display_h, fmt = _detect_fb()
    print(
        f"fb geometry: {display_w}x{display_h} @ {fmt} "
        f"(/sys/class/graphics/fb0/bits_per_pixel)"
    )

    # Render the slide at sign-native resolution. White-on-black so
    # it pops against any incidental fb residue and is unmistakable
    # on the eyeball check.
    print(f"rendering '{TEXT}' at {SIGN_W}x{SIGN_H}…")
    png = render_text_slide_png(
        TEXT,
        SIGN_W,
        SIGN_H,
        fg="#FFFFFF",
        bg="#000000",
    )
    sign_img = Image.open(io.BytesIO(png)).convert("RGB")
    frame = sign_img.tobytes()

    print(f"pushing frame to {fb} via HDMIRenderer…")
    with HDMIRenderer(
        width=SIGN_W,
        height=SIGN_H,
        display_width=display_w,
        display_height=display_h,
        output_path=fb,
        pixel_format=fmt,
    ) as r:
        r.render_frame(frame)

    print("done — text should be visible on the HDMI monitor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
