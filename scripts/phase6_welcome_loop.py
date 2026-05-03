"""Phase 6 follow-up — drive the seeded Welcome playlist through DRMRenderer.

Bring up the real PlaybackLoop on the Pi against the seeded "Welcome"
playlist via the DRM/KMS path (the fb0 path was the original target;
DRM/KMS is what makes 30 fps fades land on a Pi Zero 2 W).

What this script does:

1. Initializes ContentStorage / PlaylistStorage / ScheduleStorage at
   /home/openmarquee/data/ (Pi-side, separate from the Mac BUILD_DIR
   convention).
2. Runs `seed_if_needed` to create the Welcome → to → openMarquee
   slides + Friday-night Freedom schedule rule + bundled backgrounds /
   videos. Idempotent — re-runs are no-ops once the marker is written.
3. Opens DRMRenderer on /dev/dri/card0 — auto-detects the connector's
   preferred mode (no /sys/class/graphics fb probe needed since DRM
   tells us the active HDMI mode directly).
4. Wires PlaybackLoop with DRMRenderer + content.read_asset +
   scheduled_fetch_items (so the schedule's default fallback to Welcome
   is honored, and any Friday-night Freedom rule fires when the clock
   crosses 20:00).
5. Starts the loop and waits forever. Ctrl-C cleanly stops the loop +
   releases DRM master.

Run on the Pi (sudo for /dev/dri/card0 + DRM master):

    cd /home/openmarquee/openmarquee
    sudo PYTHONPATH=backend python3 scripts/phase6_welcome_loop.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(ROOT))

from openmarquee.content.storage import ContentStorage  # noqa: E402
from openmarquee.playback import PlaybackLoop, scheduled_fetch_items  # noqa: E402
from openmarquee.playlist import PlaylistStorage  # noqa: E402
from openmarquee.rendering.drm_kms import DRMRenderer  # noqa: E402
from openmarquee.schedule import ScheduleStorage  # noqa: E402
from openmarquee.seed import seed_if_needed  # noqa: E402

# Sign-side rasterize dims. The canonical config for HDMI deployments
# (qarl 2026-05-02 "stop thinking about low-rez for a while"; see
# memory/project_hdmi_1080p_is_primary_target.md) is 1080p sign-native
# — the GPU compositor (DRMRenderer multi-plane API + GPUSlideCompositor)
# composites at scanout via vc4 HVS, so per-frame CPU work is zero in
# the inner loop and 1080p fits the 30 fps budget cleanly. LED-matrix
# deployments (rare) can still drop these dims to e.g. 128×96 and the
# loop will fall through to the software compose_motion_frame path.
SIGN_W = 1920
SIGN_H = 1080

# How many DRM overlay planes to reserve for animated text layers.
# vc4's HDMI CRTC exposes 16-32 overlays (probed 2026-05-02), and at
# 1080p the LBM ceiling for SIMULTANEOUS active planes is ~3 with
# uncropped sources but much higher for glyph-bbox-cropped sources
# (the GPU compositor's normal mode). 8 covers any plausible Welcome
# slide (clock + ticker + breathe + a few + headroom) with memory
# cost = 8 × 8 MB = 64 MB held idle on a 512 MB Pi Zero 2 W.
MAX_ANIMATED_PLANES = 8

DATA_ROOT = Path("/home/openmarquee/data")
CONTENT_DIR = DATA_ROOT / "content"
PLAYLIST_PATH = DATA_ROOT / "playlists.json"
SCHEDULE_PATH = DATA_ROOT / "schedule.json"
SEED_MARKER = DATA_ROOT / "seed-marker.json"


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("phase6")

    card = Path("/dev/dri/card0")
    if not card.exists():
        print(f"ERR: {card} missing — DRM not available", file=sys.stderr)
        return 1

    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    content = ContentStorage(CONTENT_DIR)
    playlist_storage = PlaylistStorage(PLAYLIST_PATH)
    schedule_storage = ScheduleStorage(SCHEDULE_PATH, playlist_storage=playlist_storage)

    created = seed_if_needed(
        content,
        playlist_storage,
        SEED_MARKER,
        SIGN_W,
        SIGN_H,
        schedule_storage=schedule_storage,
    )
    log.info("seed: %d items created (no-op if marker exists)", len(created))

    items = list(content.list_all())
    log.info(
        "content store: %d items (%s)",
        len(items),
        ", ".join(sorted({it.type for it in items})),
    )
    pl = playlist_storage.load()
    log.info("default playlist: %d items", len(pl.item_ids))

    def fetch_items():
        return scheduled_fetch_items(
            content,
            playlist_storage,
            schedule_storage,
            datetime.now(UTC),
        )

    with DRMRenderer(
        width=SIGN_W, height=SIGN_H, device_path=card,
        max_animated_planes=MAX_ANIMATED_PLANES,
    ) as renderer:
        log.info(
            "DRM: %dx%d display @ %s — primary plane HVS-scaled from %dx%d, "
            "%d animated planes reserved for GPU compositor",
            renderer.display_width, renderer.display_height,
            renderer.pixel_format, SIGN_W, SIGN_H,
            MAX_ANIMATED_PLANES,
        )
        loop = PlaybackLoop(
            renderer=renderer,
            fetch_items=fetch_items,
            read_asset=content.read_asset,
        )
        await loop.start()
        log.info("playback loop started — Ctrl-C to stop")
        try:
            await loop._task  # type: ignore[union-attr]
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            log.info("stopping playback loop…")
            await loop.stop()
            log.info("done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        # asyncio.run wraps the SIGINT in CancelledError; the explicit
        # KeyboardInterrupt fallthrough still gives a clean shell exit.
        sys.exit(130)
