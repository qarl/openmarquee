"""First-boot content seeding — so a fresh device isn't a blank slate.

Contract:

- `seed_if_needed(storage, playlist_storage, marker_path, width, height)`
  is called once at app startup.
- It runs ONLY if both:
    (a) no seed-marker file exists at `marker_path`, AND
    (b) the content storage has no items.
- If it runs, seed registers:
    1. Any bundled curated backgrounds from `seed_assets/backgrounds/`
       (committed to git; generated once via
       `scripts/generate-seed-backgrounds.py` calling Pollinations.ai).
       These are the nicer "shipped with the product" backgrounds.
    2. A handful of fallback Pillow-generated gradients, but ONLY when
       no bundled backgrounds are available — keeps a freshly-flashed
       SD card without the asset directory from ending up blank.
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

from PIL import ImageDraw, ImageFont

from openmarquee.content import ImageSlide, TextSlide, VideoSlide
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


def _default_bundled_backgrounds_dir() -> Path:
    """Default location of curated backgrounds shipped in the Python
    package (committed to git by scripts/generate-seed-backgrounds.py)."""
    return Path(__file__).resolve().parent / "seed_assets" / "backgrounds"


def _name_from_filename(stem: str) -> str:
    """'brick-wall' → 'Background — Brick Wall'."""
    words = [w.capitalize() for w in stem.replace("_", "-").split("-") if w]
    return "Background — " + " ".join(words) if words else "Background"


def seed_if_needed(
    storage: ContentStorage,
    playlist_storage: PlaylistStorage,
    marker_path: Path,
    width: int,
    height: int,
    demo_video_path: Path | None = None,
    bundled_backgrounds_dir: Path | None = None,
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
        # 1. Backgrounds become *available content* (saved, but NOT auto-
        #    appended to the default playlist). Operators drag them onto the
        #    playlist track themselves. Falls back to Pillow-gradient presets
        #    when no curated pack is bundled so a stripped image isn't blank.
        bg_dir = (
            bundled_backgrounds_dir
            if bundled_backgrounds_dir is not None
            else _default_bundled_backgrounds_dir()
        )
        bundled = _seed_bundled_backgrounds(storage, bg_dir, width, height)
        created.extend(bundled)

        if not bundled:
            for preset in _SEED_PRESETS:
                png = render_gradient_png(preset, width, height)
                slide = ImageSlide(name=preset.name, duration_ms=5000)
                storage.save_image(slide, png)
                created.append(slide)

        # 2. Demo video: optional, best-effort, also NOT auto-appended
        #    (operator drags it into the playlist when they want it).
        demo = _seed_demo_video_if_available(storage, demo_video_path, width, height)
        if demo is not None:
            created.append(demo)

        # 3. The ONE thing we auto-append to the default playlist: the
        #    Welcome text slide. A fresh device boots playing "Welcome" on
        #    a nice background so the sign isn't a black screen until the
        #    operator does anything.
        welcome = _seed_welcome_slide(storage, width, height)
        created.append(welcome)
        playlist.append(welcome.id)

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


def _seed_bundled_backgrounds(
    storage: ContentStorage,
    directory: Path,
    width: int,
    height: int,
) -> list[ImageSlide]:
    """Register each image under `directory` as an ImageSlide, verbatim.

    Sorted by filename so the seed order is deterministic across boots.
    Bytes are copied as-is — no device-side resampling — so the bundled
    assets are the on-disk originals. The playback engine cover-fits down
    to the panel dims on slide entry, so a resolution change is a no-op
    for seeded content. `width` / `height` are accepted for API parity
    with older callers; they're unused here.
    """
    del width, height  # signature-compat with pre-originals callers
    if not directory.is_dir():
        return []

    created: list[ImageSlide] = []
    candidates = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )
    for path in candidates:
        try:
            raw = path.read_bytes()
            # Structural check — bad bytes in the bundled pack shouldn't
            # end up on disk where the playback engine would later have
            # to log + skip them. Pillow.verify() is fast and catches
            # truncation / magic-number mismatches without a full decode.
            with Image.open(BytesIO(raw)) as probe:
                probe.verify()
        except Exception:
            logger.exception("seed: skipping unreadable background %s", path)
            continue
        slide = ImageSlide(name=_name_from_filename(path.stem), duration_ms=5000)
        storage.save_image(slide, raw)
        created.append(slide)
    return created


# --- Welcome slide ---

# Chosen for high contrast + a warm, inviting feel at sign sizes. White
# on deep teal reads well at both LED-matrix and HDMI resolutions.
WELCOME_TEXT = "Welcome"
WELCOME_TEXT_COLOR = "#FFFFFF"
WELCOME_BG_COLOR = "#0A3D4A"


def render_welcome_png(width: int, height: int) -> bytes:
    """Flatten the Welcome slide to a PNG at the panel's native dimensions.

    Mirrors what the UI's text-slide editor does client-side — solid
    background + centered text — so the device has a ready-to-render
    PNG the moment the seed finishes.
    """
    img = Image.new("RGB", (width, height), WELCOME_BG_COLOR)
    draw = ImageDraw.Draw(img)
    font_size_px = max(12, int(height * 0.4))
    # PIL's default truetype lookup is unreliable across install paths;
    # fall back to the bitmap default when no scalable face is available.
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", font_size_px)
    except OSError:
        try:
            font = ImageFont.truetype("Arial Bold.ttf", font_size_px)
        except OSError:
            font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), WELCOME_TEXT, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((width - tw) / 2 - bbox[0], (height - th) / 2 - bbox[1]),
        WELCOME_TEXT,
        fill=WELCOME_TEXT_COLOR,
        font=font,
    )
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _seed_welcome_slide(
    storage: ContentStorage, width: int, height: int
) -> TextSlide:
    """Create + persist the 'Welcome' TextSlide that a fresh device boots
    into. Stored as a TextSlide (not ImageSlide) so the operator can open
    it in the text editor and re-skin it without starting from scratch."""
    png = render_welcome_png(width, height)
    slide = TextSlide(
        name="Welcome",
        text=WELCOME_TEXT,
        text_color=WELCOME_TEXT_COLOR,
        background_color=WELCOME_BG_COLOR,
        font_size_px=max(12, int(height * 0.4)),
        duration_ms=5000,
    )
    storage.save_text_slide(slide, png)
    return slide


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
