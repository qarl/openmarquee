"""Phase 6 follow-up — drive the seeded Welcome playlist through HDMIRenderer.

Scope-creep beyond the original Phase 6 exit (one frame on screen,
which 704e1b3 + scripts/phase6_hdmi_smoke.py landed): bring up the
real PlaybackLoop on the Pi against the seeded "Welcome" playlist.

What this script does:

1. Initializes ContentStorage / PlaylistStorage / ScheduleStorage at
   /home/openmarquee/data/ (Pi-side, separate from the Mac BUILD_DIR
   convention).
2. Runs `seed_if_needed` to create the Welcome → to → openMarquee
   slides + Friday-night Freedom schedule rule + bundled backgrounds /
   videos. Idempotent — re-runs are no-ops once the marker is written.
3. Detects fb geometry from /sys/class/graphics/fb0 (bpp → pixel_format,
   virtual_size → display dims) — same probe as phase6_hdmi_smoke.py.
4. Wires PlaybackLoop with HDMIRenderer + content.read_asset +
   scheduled_fetch_items (so the schedule's default fallback to Welcome
   is honored, and any Friday-night Freedom rule fires when the clock
   crosses 20:00).
5. Starts the loop and waits forever. Ctrl-C cleanly stops the loop +
   closes the renderer's fb fd.

Run on the Pi (sudo for /dev/fb0 write access):

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
from openmarquee.rendering.hdmi import HDMIRenderer  # noqa: E402
from openmarquee.schedule import ScheduleStorage  # noqa: E402
from openmarquee.seed import seed_if_needed  # noqa: E402

# Sign-side rasterize dims. The asset PNGs are written at this
# resolution; HDMIRenderer's NEAREST upscale + letterbox stretches
# 128×96 to whatever the HDMI display is. Matches phase6_hdmi_smoke
# so the cycle visually inherits the smoke test's framing.
SIGN_W = 128
SIGN_H = 96

DATA_ROOT = Path("/home/openmarquee/data")
CONTENT_DIR = DATA_ROOT / "content"
PLAYLIST_PATH = DATA_ROOT / "playlists.json"
SCHEDULE_PATH = DATA_ROOT / "schedule.json"
SEED_MARKER = DATA_ROOT / "seed-marker.json"


def detect_fb() -> tuple[int, int, str]:
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


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("phase6")

    fb = Path("/dev/fb0")
    if not fb.exists():
        print(f"ERR: {fb} missing — is the HDMI monitor connected?", file=sys.stderr)
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

    display_w, display_h, fmt = detect_fb()
    log.info("fb: %dx%d @ %s", display_w, display_h, fmt)

    renderer = HDMIRenderer(
        width=SIGN_W,
        height=SIGN_H,
        display_width=display_w,
        display_height=display_h,
        output_path=fb,
        pixel_format=fmt,
    )

    def fetch_items():
        # `scheduled_fetch_items` honors the Friday-night Freedom rule
        # if the schedule has one, otherwise falls through to Welcome.
        # Passing the loop in lets it stamp current_playlist_id, but
        # there's no UI consuming that here — None is fine.
        return scheduled_fetch_items(
            content,
            playlist_storage,
            schedule_storage,
            datetime.now(UTC),
        )

    loop = PlaybackLoop(
        renderer=renderer,
        fetch_items=fetch_items,
        read_asset=content.read_asset,
    )

    await loop.start()
    log.info("playback loop started — Ctrl-C to stop")
    try:
        # Block until the loop's task finishes (it won't on its own —
        # only Ctrl-C / SIGTERM ends the wait).
        await loop._task  # type: ignore[union-attr]
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        log.info("stopping playback loop…")
        await loop.stop()
        renderer.close()
        log.info("done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        # asyncio.run wraps the SIGINT in CancelledError; the explicit
        # KeyboardInterrupt fallthrough still gives a clean shell exit.
        sys.exit(130)
