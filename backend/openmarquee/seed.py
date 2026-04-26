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

from PIL import Image, ImageDraw, ImageFont

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
    # "<name> — Background" so they sort lexically grouped by material in
    # the composer's picker (e.g. "Sunset — Background" next to "Sunset
    # Gradient — Background") rather than all collapsing under a shared
    # "Background —" prefix. The composer is the intended consumer —
    # these aren't ready-to-display slides on their own.
    SeedPreset("Sunset — Background", top=(255, 87, 34), bottom=(255, 204, 0)),
    SeedPreset("Midnight — Background", top=(15, 28, 74), bottom=(0, 0, 0)),
    SeedPreset("Forest — Background", top=(34, 99, 70), bottom=(12, 30, 24)),
    SeedPreset("Ocean — Background", top=(10, 60, 120), bottom=(5, 25, 55)),
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


def _default_bundled_videos_dir() -> Path:
    """Default location of curated demo videos shipped in the Python
    package. Each .mp4 is paired with a {stem}.png thumbnail — both are
    copied verbatim into storage (no transcode at boot)."""
    return Path(__file__).resolve().parent / "seed_assets" / "videos"


def _title_from_filename(stem: str) -> str:
    """'happy-hour' → 'Happy Hour'; 'open-sign' → 'Open Sign'."""
    words = [w.capitalize() for w in stem.replace("_", "-").split("-") if w]
    return " ".join(words)


def _name_from_filename(stem: str) -> str:
    """'brick-wall' → 'Brick Wall — Background'."""
    title = _title_from_filename(stem)
    return title + " — Background" if title else "Background"


def seed_if_needed(
    storage: ContentStorage,
    playlist_storage: PlaylistStorage,
    marker_path: Path,
    width: int,
    height: int,
    demo_video_path: Path | None = None,
    bundled_backgrounds_dir: Path | None = None,
    bundled_videos_dir: Path | None = None,
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

        # 2. Bundled videos: scan seed_assets/videos/ for {name}.mp4 paired
        #    with {name}.png (thumbnail). Each pair becomes a VideoSlide;
        #    none of them are auto-appended to the playlist.
        videos_dir = (
            bundled_videos_dir if bundled_videos_dir is not None else _default_bundled_videos_dir()
        )
        created.extend(_seed_bundled_videos(storage, videos_dir))

        # 2b. Legacy single-file demo slot — retained for out-of-band
        #     drops via OPENMARQUEE_DEMO_VIDEO_PATH. Skipped when absent.
        demo = _seed_demo_video_if_available(storage, demo_video_path, width, height)
        if demo is not None:
            created.append(demo)

        # 3. What we auto-append to the default playlist: three text
        #    slides reading "Welcome" → "to" → "openMarquee". A fresh
        #    device plays them in order so the sign isn't a black screen
        #    until the operator does anything. Each pairs a bundled
        #    background + a distinct font + transition so the demo
        #    shows off the editor's range immediately.
        welcome_slides = _seed_welcome_playlist_slides(
            storage,
            width,
            height,
            bundled_backgrounds_dir=bg_dir,
            bundled_bg_slides=bundled,
        )
        created.extend(welcome_slides)
        for slide, spec in zip(welcome_slides, _WELCOME_SPECS, strict=True):
            playlist.append(slide.id, transition=spec.transition_out)

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
        p
        for p in directory.iterdir()
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


def _seed_bundled_videos(storage: ContentStorage, directory: Path) -> list[VideoSlide]:
    """Register each {name}.mp4 / {name}.png pair under `directory` as
    a VideoSlide, bytes copied verbatim.

    Missing thumbnail or missing MP4 = that pair is skipped with a
    log line — a half-filled bundle shouldn't kill the rest of the
    seed. Sorted so boot order is deterministic across devices.
    """
    if not directory.is_dir():
        return []

    created: list[VideoSlide] = []
    mp4s = sorted(p for p in directory.iterdir() if p.suffix.lower() == ".mp4")
    for mp4_path in mp4s:
        png_path = mp4_path.with_suffix(".png")
        if not png_path.exists():
            logger.warning(
                "seed: skipping %s — missing paired thumbnail %s",
                mp4_path.name,
                png_path.name,
            )
            continue
        try:
            mp4_bytes = mp4_path.read_bytes()
            thumbnail = png_path.read_bytes()
            # Structural check — both must parse cleanly. An MP4 whose
            # first-byte header is bad would otherwise reach the playback
            # loop and crash ffmpeg; better to skip + log here.
            if len(mp4_bytes) < 12 or mp4_bytes[4:8] != b"ftyp":
                raise ValueError("not a valid MP4 (no ftyp box)")
            with Image.open(BytesIO(thumbnail)) as probe:
                probe.verify()
        except Exception:
            logger.exception("seed: skipping unreadable video %s", mp4_path)
            continue
        slide = VideoSlide(
            name=_title_from_filename(mp4_path.stem) or "Video",
            duration_ms=10_000,
        )
        storage.save_video(slide, thumbnail, mp4_bytes)
        created.append(slide)
    return created


# --- Welcome playlist ---

# Legacy two-color fallback (used when bundled backgrounds aren't present
# and we can't composite a real image). High contrast + warm feel.
WELCOME_TEXT_COLOR = "#FFFFFF"
WELCOME_BG_COLOR = "#0A3D4A"


# Three-slide intro: each slide pairs a bundled background with a
# distinct font + transition so the demo shows off the editor's range
# the moment the device boots. Background lookups are by *base name*
# of the bundled file (chalkboard.png → "Chalkboard"); the seed only
# wires a slide if the matching background was successfully seeded.
@dataclass
class _WelcomeSlideSpec:
    text: str
    font_family: str  # matches editor.js FONT_FAMILIES
    text_color: str
    background_filename_stem: str  # e.g. "chalkboard"
    transition_out: str  # transition to play after this slide


_WELCOME_SPECS: tuple[_WelcomeSlideSpec, ...] = (
    _WelcomeSlideSpec(
        text="Welcome",
        font_family="Caveat Brush",
        text_color="#FFFFFF",
        background_filename_stem="chalkboard",
        transition_out="fade",
    ),
    _WelcomeSlideSpec(
        text="to",
        font_family="Sedgwick Ave Display",
        text_color="#000000",
        background_filename_stem="brick-wall",
        transition_out="wipe",
    ),
    _WelcomeSlideSpec(
        text="openMarquee",
        font_family="Pacifico",
        # Amber from the marketing site — reads as "neon at night" on midnight bg.
        text_color="#F5A524",
        background_filename_stem="midnight",
        transition_out="iris",
    ),
)


_WELCOME_PLAYLIST_TEXTS: tuple[str, ...] = tuple(s.text for s in _WELCOME_SPECS)


def render_welcome_png(width: int, height: int) -> bytes:
    """PNG for the first 'Welcome' slide at the panel's native dims. Kept
    as a thin shim for tests + historical callers; new code should prefer
    render_text_slide_png()."""
    return render_text_slide_png("Welcome", width, height)


def render_text_slide_png(
    text: str,
    width: int,
    height: int,
    fg: str = WELCOME_TEXT_COLOR,
    bg: str = WELCOME_BG_COLOR,
    *,
    background_image_path: Path | None = None,
    font_family: str | None = None,
) -> bytes:
    """Flatten one centered-text slide to a PNG.

    Mirrors what the UI's text-slide editor does client-side — background
    (solid color OR cover-fit image) + centered text — so the device has
    a ready-to-render PNG the moment the seed finishes. Font auto-shrinks
    if the text would overflow 90% of the canvas width (e.g. "openMarquee"
    at small panels).

    `background_image_path` overrides `bg` when provided. `font_family`
    looks up a bundled TTF from `_BUNDLED_FONT_FILES` (matches the UI
    editor's named families); falls back to DejaVu Sans when None or
    the family isn't bundled.
    """
    if background_image_path is not None and background_image_path.exists():
        with Image.open(background_image_path) as src:
            img = _cover_fit(src.convert("RGB"), width, height)
    else:
        img = Image.new("RGB", (width, height), bg)

    draw = ImageDraw.Draw(img)
    font_size_px = max(12, int(height * 0.4))
    font = _load_text_font(font_family, font_size_px)
    while font_size_px > 12:
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= width * 0.9:
            break
        font_size_px -= 4
        font = _load_text_font(font_family, font_size_px)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((width - tw) / 2 - bbox[0], (height - th) / 2 - bbox[1]),
        text,
        fill=fg,
        font=font,
    )
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _cover_fit(image: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Scale + center-crop `image` to exactly (target_w, target_h)."""
    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w = max(1, round(src_w * scale))
    new_h = max(1, round(src_h * scale))
    resized = image.resize((new_w, new_h), resample=Image.Resampling.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def _load_text_font(family: str | None, size_px: int):
    """Load a bundled @font-face TTF when `family` matches an entry in
    `auto_render._BUNDLED_FONT_FILES`; otherwise the historical bold
    fallback (DejaVuSans-Bold → Arial Bold → bitmap)."""
    if family:
        try:
            from openmarquee.auto_render import (
                _BUNDLED_FONT_FILES,
                _bundled_fonts_dir,
            )

            bundled_name = _BUNDLED_FONT_FILES.get(family)
            if bundled_name:
                path = _bundled_fonts_dir() / bundled_name
                if path.exists():
                    try:
                        return ImageFont.truetype(str(path), size_px)
                    except OSError:
                        pass
        except Exception:
            pass
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size_px)
    except OSError:
        try:
            return ImageFont.truetype("Arial Bold.ttf", size_px)
        except OSError:
            return ImageFont.load_default()


def _seed_welcome_playlist_slides(
    storage: ContentStorage,
    width: int,
    height: int,
    bundled_backgrounds_dir: Path | None = None,
    bundled_bg_slides: list[ImageSlide] | None = None,
) -> list[TextSlide]:
    """Create the three intro text slides for the default playlist.

    Each spec in `_WELCOME_SPECS` pairs a font, color, and bundled
    background. When the matching background is present we composite it
    into the rasterized PNG AND wire the TextSlide's
    `background_image_slide_id` to the seeded ImageSlide so the operator
    can re-edit without losing the background. Falls back to a solid
    color when the bundled asset is missing.
    """
    bg_dir = bundled_backgrounds_dir or _default_bundled_backgrounds_dir()
    # Bundled image slides are named "<Title> — Background" — index by
    # the same filename stem each spec references for an O(1) lookup.
    bg_slides_by_stem: dict[str, ImageSlide] = {}
    for slide in bundled_bg_slides or ():
        for cand in bg_dir.iterdir() if bg_dir.is_dir() else ():
            if _name_from_filename(cand.stem) == slide.name:
                bg_slides_by_stem[cand.stem] = slide
                break

    slides: list[TextSlide] = []
    font_size_px = max(12, int(height * 0.4))
    for spec in _WELCOME_SPECS:
        bg_path = bg_dir / f"{spec.background_filename_stem}.png"
        bg_image_slide = bg_slides_by_stem.get(spec.background_filename_stem)
        png = render_text_slide_png(
            spec.text,
            width,
            height,
            fg=spec.text_color,
            bg=WELCOME_BG_COLOR,
            background_image_path=bg_path if bg_path.exists() else None,
            font_family=spec.font_family,
        )
        slide = TextSlide(
            name=spec.text,
            text=spec.text,
            text_color=spec.text_color,
            background_color=WELCOME_BG_COLOR,
            font_family=spec.font_family,
            font_size_px=font_size_px,
            duration_ms=3000,
            background_image_slide_id=bg_image_slide.id if bg_image_slide else None,
        )
        storage.save_text_slide(slide, png)
        slides.append(slide)
    return slides


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
