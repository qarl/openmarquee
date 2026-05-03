"""Phase 6.5 DRM/KMS smoke test — first frame via DRM/KMS direct.

Parallel to phase6_hdmi_smoke.py but targets the new DRMRenderer
(legacy SETCRTC mode-set + dumb buffer + mmap'd framebuffer).
Validates that the ctypes wrapper survives every ioctl handshake,
that the resulting frame actually appears on screen, and gives us
a fps measurement for the post-mode-set per-frame path.

Run on the Pi (sudo for /dev/dri/card0 RDWR + DRM master):

    cd /home/openmarquee/openmarquee
    sudo PYTHONPATH=backend python3 scripts/phase6_drm_smoke.py
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from openmarquee.rendering.drm_kms import DRMRenderer  # noqa: E402
from openmarquee.seed import render_text_slide_png  # noqa: E402

# 1080p canonical config (qarl 2026-05-02 architectural pivot; see
# memory/project_hdmi_1080p_is_primary_target.md). LED-matrix smokes
# can drop to 128x96 if needed.
SIGN_W = 1920
SIGN_H = 1080
TEXT = "HELLO DRM"


def main() -> int:
    card = Path("/dev/dri/card0")
    if not card.exists():
        print(f"ERR: {card} missing — DRM not available", file=sys.stderr)
        return 1

    # Render the slide at sign-native resolution.
    print(f"rendering '{TEXT}' at {SIGN_W}x{SIGN_H}…")
    png = render_text_slide_png(
        TEXT, SIGN_W, SIGN_H, fg="#FFFFFF", bg="#003366"
    )
    sign_img = Image.open(io.BytesIO(png)).convert("RGB")
    frame = sign_img.tobytes()

    print(f"opening DRM renderer ({card})…")
    with DRMRenderer(width=SIGN_W, height=SIGN_H, device_path=card) as r:
        print(f"display: {r.display_width}x{r.display_height}")
        print("pushing first frame…")
        r.render_frame(frame)
        print("first frame on screen — measuring fps over 10 renders…")
        t0 = time.perf_counter()
        for _ in range(10):
            r.render_frame(frame)
        elapsed = time.perf_counter() - t0
        per_frame = (elapsed / 10) * 1000
        print(f"  per-frame: {per_frame:.1f}ms  ({1000/per_frame:.1f} fps)")
        # Hold the frame visible for a moment so qarl can eyeball it
        # before the program exits and the kernel restores the previous
        # CRTC state.
        print("holding frame for 5s — eyeball check now…")
        time.sleep(5)

    print("done — DRM master released; original CRTC restored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
