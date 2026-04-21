"""First-boot content seeding — so a fresh device isn't a blank slate.

Contract:

- `seed_if_needed(storage, playlist_storage, marker_path, width, height)`
  is called once at app startup.
- It runs ONLY if both:
    (a) no seed-marker file exists at `marker_path`, AND
    (b) the content storage has no items.
- If it runs, it generates a handful of gradient-background ImageSlide
  entries (produced locally via Pillow so this works offline on a
  freshly-flashed Pi with no internet), persists them, appends them to
  the default playlist, and writes the marker so re-running is a no-op.
- If the operator later deletes everything they got, the marker stays,
  so boot doesn't re-seed behind their back.

In addition to gradients, seed also picks up a **demo video** if one
is present at `OPENMARQUEE_DEMO_VIDEO_PATH` (default: a bundled path
under `openmarquee/seed_assets/demo.mp4`). The actual MP4 is
provisioned out-of-band (see `scripts/download-demo-video.sh` for a
CC-BY Blender Foundation clip) rather than committed to git — the
architecture is here so flashed SD images + fresh clones can both
light up the full seed experience when the asset is in place.

For today's captive-portal UX, Pillow-generated gradients are good
enough: they give an operator something to hit Play on immediately,
without waiting for them to upload their first slide.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image

from openmarquee.content import ImageSlide, VideoSlide
from openmarquee.content.storage import ContentStorage
from openmarquee.playlist import PlaylistStorage

logger = logging.getLogger(__name__)

# Bump alongside any change to the curated seed list so a redeployed device
# that already ran seeding can know there's a new set to offer. Today we
# just record this on the marker for forensic value; future code could
# diff versions and top up missing items.
SEED_VERSION = 1


@dataclass(frozen=True)
class SeedPreset:
    """One entry in the shipped seed set."""

    name: str
    # Two hex colors describing a vertical gradient. Simple but signage-y.
    top: tuple[int, int, int]
    bottom: tuple[int, int, int]


_SEED_PRESETS: tuple[SeedPreset, ...] = (
    # "Background — <name>" so they surface as obvious background candidates
    # in the composer's "From saved slide" picker. The composer is the
    # intended consumer — these aren't ready-to-display slides on their own.
    SeedPreset("Background — Sunset", top=(255, 87, 34), bottom=(255, 204, 0)),
    SeedPreset("Background — Midnight", top=(15, 28, 74), bottom=(0, 0, 0)),
    SeedPreset("Background — Forest", top=(34, 99, 70), bottom=(12, 30, 24)),
    SeedPreset("Background — Ocean", top=(10, 60, 120), bottom=(5, 25, 55)),
)


def render_gradient_png(preset: SeedPreset, width: int, height: int) -> bytes:
    """Return PNG bytes for a vertical gradient background.

    Rendered as a 1-pixel-wide column stretched horizontally — the inner
    color interpolation is O(H) pure Python and the horizontal stretch is
    C-internal in PIL's `resize`. Stays fast even at 1080p so first-boot
    doesn't stall the lifespan on a large HDMI configuration.
    """
    column = Image.new("RGB", (1, height))
    for y in range(height):
        t = y / max(1, height - 1)
        r = round(preset.top[0] + (preset.bottom[0] - preset.top[0]) * t)
        g = round(preset.top[1] + (preset.bottom[1] - preset.top[1]) * t)
        b = round(preset.top[2] + (preset.bottom[2] - preset.top[2]) * t)
        column.putpixel((0, y), (r, g, b))
    img = column.resize((width, height), Image.Resampling.NEAREST)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def seed_if_needed(
    storage: ContentStorage,
    playlist_storage: PlaylistStorage,
    marker_path: Path,
    width: int,
    height: int,
    demo_video_path: Path | None = None,
) -> list:
    """Run the first-boot seed if appropriate; return the list of items
    that were created (empty if the call was a no-op).

    `demo_video_path` is optional. When present and pointing at a readable
    MP4 with a well-formed ftyp box, a VideoSlide is seeded alongside the
    gradient backgrounds. Absent / unreadable / mis-formed files are
    silently skipped — the gradients still land. This lets a fresh SD
    image bundle a demo clip (provisioned by scripts/download-demo-video.sh
    or a pi-gen step) without making the seed flow fragile.
    """
    marker_path = Path(marker_path)
    if marker_path.exists():
        return []

    existing = storage.list_all()
    if existing:
        # Operator already has content — don't touch it. Stamp the marker
        # so we never try again on this device.
        _write_marker(marker_path, created=0, reason="content-already-present")
        return []

    playlist = playlist_storage.load()
    if playlist.item_ids:
        # Defensive: an empty store + non-empty playlist is an odd state
        # (operator manually deleted all content but kept the playlist
        # JSON) — don't append our seed items onto theirs.
        _write_marker(marker_path, created=0, reason="playlist-not-empty")
        return []

    created: list = []
    try:
        for preset in _SEED_PRESETS:
            png = render_gradient_png(preset, width, height)
            slide = ImageSlide(name=preset.name, duration_ms=5000)
            storage.save_image(slide, png)
            playlist.append(slide.id)
            created.append(slide)

        # Demo video: optional, best-effort. Any failure (missing / bad
        # bytes / unreadable) falls through silently; the gradients are
        # still persisted and the seed is considered successful.
        demo = _seed_demo_video_if_available(storage, demo_video_path, width, height)
        if demo is not None:
            playlist.append(demo.id)
            created.append(demo)

        playlist_storage.save(playlist)
    except Exception:
        logger.exception("seed: failed while creating starter slides")
        # Roll back any already-saved items so a half-seeded disk doesn't
        # become permanent state the next boot would mis-interpret as
        # "content-already-present".
        for slide in created:
            try:
                storage.delete(slide.id)
            except Exception:
                # Best-effort cleanup; if even delete fails (disk really
                # gone) the operator will have orphans, but the marker
                # stays absent so the next boot retries.
                logger.warning("seed: could not roll back %s", slide.id)
        # Marker stays absent on failure — next boot gets another shot.
        raise

    _write_marker(marker_path, created=len(created), reason="fresh-install")
    logger.info("seed: created %d starter slides", len(created))
    return created


def _seed_demo_video_if_available(
    storage: ContentStorage,
    demo_video_path: Path | None,
    width: int,
    height: int,
) -> VideoSlide | None:
    """Best-effort: register a VideoSlide for the demo clip if the file
    exists and looks like an MP4. Returns None on any failure (the main
    seed flow treats that as "no demo bundled with this build")."""
    if demo_video_path is None:
        return None
    path = Path(demo_video_path)
    if not path.is_file():
        logger.info("seed: no demo video at %s; skipping", path)
        return None
    try:
        mp4_bytes = path.read_bytes()
        # Same negative filter as the upload API: MP4s start with a size
        # field then `ftyp` at offset 4. A tuned seed can't trust the
        # filename alone (the operator might have swapped in a .mov).
        if len(mp4_bytes) < 12 or mp4_bytes[4:8] != b"ftyp":
            logger.warning("seed: demo video at %s is not an MP4; skipping", path)
            return None

        # Generate a generic dark-gradient thumbnail — we can't decode the
        # MP4 without ffmpeg, and the video endpoint is what actually plays
        # anyway. A thumbnail extracted at upload time would be better;
        # that's a nice follow-up when the ffmpeg.wasm spike lands.
        thumbnail = render_gradient_png(
            SeedPreset("Demo", top=(30, 30, 40), bottom=(5, 5, 10)),
            width,
            height,
        )
        video = VideoSlide(name="Demo — sample clip", duration_ms=10_000)
        storage.save_video(video, thumbnail, mp4_bytes)
        return video
    except Exception:
        logger.exception("seed: failed to seed demo video from %s", path)
        return None


def _write_marker(path: Path, *, created: int, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "seed_version": SEED_VERSION,
        "created": created,
        "reason": reason,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)
