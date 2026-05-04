#!/usr/bin/env python3
"""Phase 7 shared-fd smoke — DRMRenderer and ShaderRenderer cooperating.

Validates the last piece of infrastructure before PlaybackLoop wire-up
(#196): a multi-plane DRMRenderer holds DRM master + drives steady-
state scanout, and a ShaderRenderer is constructed with the
DRMRenderer's fd to run a transition WITHOUT a re-master dance.

Sequence:

  1. DRMRenderer enters (atomic mode-set, primary plane bound).
  2. Render frame A (a warm gradient slide) via DRMRenderer.
  3. Hold for ~1 s.
  4. ShaderRenderer enters with drm_fd=DRMRenderer.drm_fd. Plane
     discovery runs against the shared fd; SetCrtc/PageFlip during the
     transition issues under DRMRenderer's master authorization.
  5. Run an iris transition from frame A to frame B over 2 s. Each
     ShaderRenderer commit_frame writes a fresh GBM-backed fb to the
     primary plane.
  6. DRMRenderer paints frame B into its primary dumb buffer.
  7. DRMRenderer.restage_primary_fb() stages primary FB_ID + CRTC
     binding (required: ShaderRenderer's legacy SetCrtc'd through the
     atomic-property layer's back, so our _pending_props is empty
     without an explicit restage and commit() would be a no-op).
  8. DRMRenderer.commit() atomic-rebinds primary plane to OUR fb.
     CRTC swaps off shader's last fb to DRMRenderer's fb in one
     vblank — clean handoff, no kernel implicit-pin window.
  9. ShaderRenderer.close() — RmFB's its last fb (now idle, kernel
     released the implicit ref when our atomic-commit displaced it),
     tears down GL/EGL/GBM. Does NOT close the fd or blank the CRTC.
  10. Hold frame B for ~1 s, then close DRMRenderer.

Run on the dev Pi as `openmarquee` user. Welcome loop must be stopped
first:

  sudo killall -9 python3
  cd /home/openmarquee/openmarquee
  sudo PYTHONPATH=backend python3 scripts/phase7_shared_fd_smoke.py
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from openmarquee.rendering.drm_kms import DRMRenderer  # noqa: E402
from openmarquee.rendering.shader_compositor import ShaderRenderer  # noqa: E402

log = logging.getLogger("phase7_shared_fd_smoke")

# Same dims the welcome loop runs at — rgb565 primary plane, 8 animated
# plane slots reserved (we don't use them in this smoke, just match
# the welcome-loop config so the renderer state is realistic).
SIGN_W = 1920
SIGN_H = 1080
MAX_ANIMATED_PLANES = 8

_FADE_FPS = 30


def _build_frame_a(w: int, h: int) -> bytes:
    """Warm gradient — RGB888 row-major top-down for DRMRenderer."""
    xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
    ys = np.linspace(0.0, 1.0, h, dtype=np.float32)
    r = np.broadcast_to(((1.0 - ys) * 220 + 30).astype(np.uint8)[:, None], (h, w))
    g = np.broadcast_to((ys * 90 + 30).astype(np.uint8)[:, None], (h, w))
    b = np.broadcast_to((ys * 50 + 20).astype(np.uint8)[:, None], (h, w))
    return np.stack([r, g, b], axis=-1).tobytes()


def _build_frame_b(w: int, h: int) -> bytes:
    """Cool blue gradient — RGB888 row-major top-down."""
    xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
    ys = np.linspace(0.0, 1.0, h, dtype=np.float32)
    r = np.broadcast_to((ys * 40 + 5).astype(np.uint8)[:, None], (h, w))
    g = np.broadcast_to((ys * 60 + 30).astype(np.uint8)[:, None], (h, w))
    b = np.broadcast_to(((1.0 - ys) * 130 + 80).astype(np.uint8)[:, None], (h, w))
    return np.stack([r, g, b], axis=-1).tobytes()


def _rgb_to_rgba(rgb: bytes, w: int, h: int) -> bytes:
    """RGBA conversion for ShaderRenderer texture upload. opaque alpha."""
    arr = np.frombuffer(rgb, dtype=np.uint8).reshape(h, w, 3)
    alpha = np.full((h, w, 1), 255, dtype=np.uint8)
    return np.concatenate([arr, alpha], axis=-1).tobytes()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--kind", default="iris", choices=("fade", "iris"))
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 1. DRMRenderer takes master + sets the mode. Same construction
    # the welcome loop uses (rgb565 primary, 8 animated plane slots
    # reserved — we don't attach overlays here, just match prod shape).
    with DRMRenderer(
        SIGN_W, SIGN_H,
        pixel_format="rgb565",
        max_animated_planes=MAX_ANIMATED_PLANES,
    ) as drm:
        log.info(
            "DRMRenderer up: %dx%d fd=%d",
            drm.width, drm.height, drm.drm_fd,
        )

        # 2-3. Render frame A, hold ~1 s.
        frame_a_rgb = _build_frame_a(drm.width, drm.height)
        drm.render_frame(frame_a_rgb)
        drm.commit()
        log.info("frame A on screen, holding 1 s...")
        time.sleep(1.0)

        # 4-5. ShaderRenderer with shared fd; iris transition A -> B.
        frame_b_rgb = _build_frame_b(drm.width, drm.height)
        from_rgba = _rgb_to_rgba(frame_a_rgb, drm.width, drm.height)
        to_rgba = _rgb_to_rgba(frame_b_rgb, drm.width, drm.height)

        shared_fd = drm.drm_fd
        assert shared_fd is not None, "DRMRenderer.drm_fd must be live"
        # Construct ShaderRenderer outside the `with` so the handoff
        # dance (paint primary, restage, atomic commit, THEN
        # shader.close()) can land in the right order.
        shader = ShaderRenderer(drm_fd=shared_fd).__enter__()
        try:
            log.info(
                "ShaderRenderer up via shared fd=%d: %dx%d",
                shared_fd, shader.width, shader.height,
            )
            shader.set_kind(args.kind)
            shader.set_from(from_rgba, drm.width, drm.height)
            shader.set_to(to_rgba, drm.width, drm.height)

            n_frames = max(1, int(args.seconds * _FADE_FPS))
            frame_dt = 1.0 / _FADE_FPS
            t0 = time.monotonic()
            for i in range(n_frames):
                t = i / max(1, n_frames - 1)
                shader.set_transition_t(t)
                shader.commit_frame()
                target = t0 + (i + 1) * frame_dt
                sleep_for = target - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)
            elapsed = time.monotonic() - t0
            log.info(
                "%s transition: %d frames in %.2fs (%.1f fps)",
                args.kind, n_frames, elapsed, n_frames / elapsed,
            )

            # 6-8. Hand the primary plane back to DRMRenderer in one
            # atomic commit BEFORE closing ShaderRenderer. Order matters:
            # ShaderRenderer's last legacy-SetCrtc bypassed
            # DRMRenderer's _pending_props, so DRMRenderer.commit()
            # alone won't re-stage primary's FB_ID. restage_primary_fb()
            # explicitly puts FB_ID + CRTC rects back in _pending_props.
            drm.render_frame(frame_b_rgb)
            drm.restage_primary_fb()
            drm.commit()
            log.info("primary plane handed back to DRMRenderer")
        finally:
            # 9. Now safe to RmFB shader's last fb — atomic commit at
            # step 8 displaced it; kernel released its implicit pin.
            shader.close()

        log.info("frame B on screen via multi-plane, holding 1 s...")
        time.sleep(1.0)

    log.info("clean teardown — display blanked, DRM master released")
    return 0


if __name__ == "__main__":
    sys.exit(main())
