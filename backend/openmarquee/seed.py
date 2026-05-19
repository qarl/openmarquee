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
import math
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from openmarquee._atomic import atomic_write_text
from openmarquee.content import (
    BackgroundPattern,
    ImageSlide,
    TextBox,
    TextLayer,
    TextSlide,
    VideoSlide,
)
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
    schedule_storage=None,
) -> list:
    """Run the first-boot seed if appropriate; return the list of items
    that were created (empty if the call was a no-op).

    `demo_video_path` is optional. When present and pointing at a readable
    MP4 with a well-formed ftyp box, a VideoSlide is seeded alongside the
    gradient backgrounds. Absent / unreadable / mis-formed files are
    silently skipped — the gradients still land. This lets a fresh SD
    image bundle a demo clip (provisioned by scripts/download-demo-video.sh
    or a pi-gen step) without making the seed flow fragile.

    `schedule_storage` is optional — when provided, the seed also writes
    the Friday-night Freedom schedule rule. Tests that don't care about
    scheduling can omit it; production wiring (app.py lifespan) always
    passes one.
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

        # 3. The first-boot demo reel — FREE YOUR SIGN. 19 slides
        #    (15 design frames; the typo + panic-flash live-edits each
        #    expand to 3 quick-cut sub-slides) escalating from a boot log
        #    through synonyms / chant / scream / silence / stadium /
        #    panic / cooldown, stitched with all 16 device transitions.
        #    Replaces the previous Welcome (3-slide) + Freedom (3-slide)
        #    seed per qarl's 2026-05-04 ask after the design handoff.
        demo_slides = _seed_demo_reel_slides(storage, width, height)
        created.extend(demo_slides)
        for slide, spec in zip(demo_slides, _DEMO_REEL, strict=True):
            playlist.append(
                slide.id,
                transition=spec.transition_out,
                transition_ms=spec.transition_ms,
            )

        # save() coerces the playlist's id + name to DEFAULT_PLAYLIST_ID +
        # "Demo" — preserves the default-playlist identity while giving
        # the operator a friendlier display name than "default".
        playlist_storage.save(playlist)

        # The previous Welcome + Freedom 2-playlist split (and its
        # Friday-night Freedom schedule rule) collapses into the demo
        # reel above. The "FREE / YOUR / SIGN" beats live in the reel
        # frames 02-04; the protest-poster vibe is captured by the reel's
        # own escalation arc.
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


# Fallback colors for the text-slide raster path when no bundled
# background is present and we can't composite a real image. High
# contrast + warm feel. (Pre-2026-05-04 these doubled as the Welcome
# trio's bg + fg defaults; post-reel-collapse they're used by
# render_text_slide_png / _draw_text_into as the no-image fallback.)
WELCOME_TEXT_COLOR = "#FFFFFF"
WELCOME_BG_COLOR = "#0A3D4A"


# ─── FREE YOUR SIGN demo reel ────────────────────────────────
# Designer-handoff bundle 2026-05-04 (claude.ai/design):
# /Users/qarl/project/openmarquee/design/free-your-sign-2026-05-04/.
# A self-running first-boot demo that escalates
#   boot → FREE → YOUR → SIGN → sentence → Liberate → UNCAGE → typo →
#   3×3 chaos → chant wall → scream → silence → stadium → panic flash → cooldown
# stitched with all 16 device transitions. Replaces the previous
# Welcome+Freedom 2-playlist seed per qarl's 2026-05-04 ask.
#
# The two "live edit" frames the designer wrote with React state-
# machines (TypoFix character-swap, PanicFlash multi-font flicker)
# are expanded here into rapid sub-slide sequences with `cut`
# transitions between them, per qarl's explicit greenlight.

@dataclass(frozen=True)
class _DemoLayer:
    """One TextLayer's worth of config for a demo reel frame."""
    text: str
    font_family: str | None = None
    text_color: str = "#FFFFFF"
    box: tuple[float, float, float, float] = (0.1, 0.1, 0.8, 0.8)
    font_size_pct: float | None = None
    font_size_px: int | None = None
    motion: str = "static"
    motion_intensity: int = 50
    motion_phase: float = 0.0
    motion_speed: float = 1.0
    blend: str = "normal"
    opacity: float = 1.0
    text_align: str = "center"


@dataclass(frozen=True)
class _DemoFrame:
    """One slide in the demo reel."""
    name: str  # display name in the slide list (numbered for ordering)
    layers: tuple[_DemoLayer, ...]
    duration_ms: int = 1000
    background_color: str = "#050608"
    background_pattern: BackgroundPattern | None = None
    transition_out: str = "cut"
    transition_ms: int = 1000


# Box helper — design uses pixel-y positions; the box model is normalized.
def _b(x, y, w, h):
    return (round(x, 4), round(y, 4), round(w, 4), round(h, 4))


# Multi-line boot-log text (fits a left-aligned box; centered horizontally
# inside the box because _draw_text_into centers).
_BOOT_LOG_TEXT = (
    "> openMarquee v0.4.2 boot\n"
    "  panel-0 . . . . . . . .  ok\n"
    "  motion-engine . . . . .  ok\n"
    "  flock-mesh . . . . . . . ok\n"
    "  9.6V / 0.41A / 27°C\n"
    "  ready."
)

# Long-string repeated text used by ticker/marquee frames so the scrolling
# never shows a gap. The motion engine wraps within the box.
# qarl 2026-05-06 reel edits: shortened to single-instance strings —
# the motion-engine ticker handles repetition visually as it scrolls.
_SCREAM_TEXT = "FREE YOUR SIGN!!!!!  "
_CHANT_TEXT = "FREE  YOUR  SIGN  "


# Reel v2 (2026-05-06):
#   * Boot moved from slot 01 → slot 15 (last); the loop wraps Boot→FREE.
#   * Durations re-budgeted with cut=0ms / non-cut=600ms transitions so
#     every readable frame has ≥800ms effective hold (qarl 2026-05-04:
#     "slide time gets eaten by transitions, so a 1 second slide is all
#     transition, you can't read it"). The math:
#       effective_hold = duration_ms − (in_trans_ms / 2) − (out_trans_ms / 2)
#   * Tile Chaos: 4-quadrant grid → 5 overlapping center-crossing layers
#     so the chaos actually reads as chaos, not as a clean grid (qarl).
#   * Uses post-bug-list slide behaviors:
#       - text_align (B4) — left-aligned terminal/code captions and the
#         long Sentence prose; center elsewhere.
#       - opacity (B10) — Stadium ticker echo at 0.4 for ghost feel,
#         Cooldown loop counter at 0.7 for a fading credits beat.
#       - motion_speed (B2) — Silence pulse slowed to 0.5x for ominous
#         dying carrier; Stadium ticker 1.4x for urgency; Cooldown
#         blink 0.5x; Boot breathing badge 0.7x for slow heartbeat.
#       - font_size_pct now correctly = % of box width (B3 spec fix).
#       - word-wrap (B4) — Sentence's longer subtitle prose flows
#         multi-line within its narrower box.

_DEMO_REEL: tuple[_DemoFrame, ...] = (
    # 1 · FREE — narrow Anton, amber, definitive.
    _DemoFrame(
        name="01 · FREE",
        duration_ms=1500,
        background_color="#050608",
        layers=(
            _DemoLayer(
                text="FREE",
                font_family="Anton",
                text_color="#FFB43C",
                box=_b(0.05, 0.1, 0.9, 0.8),
                font_size_pct=80.0,
            ),
        ),
        transition_out="wipe",
        transition_ms=600,
    ),

    # 2 · YOUR — Alfa Slab, mint green.
    _DemoFrame(
        name="02 · YOUR",
        duration_ms=1500,
        background_color="#050608",
        layers=(
            _DemoLayer(
                text="YOUR",
                font_family="Alfa Slab One",
                text_color="#5AF095",
                box=_b(0.05, 0.1, 0.9, 0.8),
                font_size_pct=70.0,
            ),
        ),
        transition_out="slide",
        transition_ms=600,
    ),

    # 3 · SIGN — Bowlby, hot pink, breathing. qarl 2026-05-06 edits:
    # bumped duration 1600→2000 + breathe intensity 35→86 (more dramatic).
    _DemoFrame(
        name="03 · SIGN",
        duration_ms=2000,
        background_color="#050608",
        layers=(
            _DemoLayer(
                text="SIGN",
                font_family="Bowlby One SC",
                text_color="#FF5FA7",
                box=_b(0.05, 0.1, 0.9, 0.8),
                font_size_pct=88.0,
                motion="breathe",
                motion_intensity=86,
            ),
        ),
        transition_out="fade",
        transition_ms=600,
    ),

    # 4 · THE SENTENCE — Playfair italic on cream. Mic-drop + a
    # left-aligned subtitle that uses word-wrap (B4) to flow across
    # multiple lines within its narrower box.
    _DemoFrame(
        name="04 · The Sentence",
        duration_ms=1800,
        background_color="#FBF3DC",
        layers=(
            _DemoLayer(
                # 14% of box width (post-B3 sizing) keeps the headline
                # to one big-but-readable line; v1's 30% reads huge and
                # word-wraps now that B4 word-wrap landed.
                text="Free your sign.",
                font_family="Playfair Display",
                text_color="#1A1610",
                box=_b(0.05, 0.22, 0.9, 0.36),
                font_size_pct=14.0,
                motion="breathe",
                motion_intensity=20,
            ),
            _DemoLayer(
                # Subtitle deliberately wraps multi-line within its
                # narrower box -- B4 word-wrap demo. qarl 2026-05-06
                # editor pass: subtitle box moved + tightened.
                text="say what you mean. mean what you say.",
                font_family="Playfair Display",
                text_color="#5A4A2E",
                box=_b(0.2493, 0.7508, 0.4415, 0.1183),
                font_size_pct=8.0,
                text_align="left",
            ),
        ),
        transition_out="cut",
        transition_ms=500,
    ),

    # 5 · LIBERATE — first synonym, dignified Playfair on cream.
    # Caption text_align=left so the // comment reads as code.
    _DemoFrame(
        name="05 · Liberate",
        duration_ms=1400,
        background_color="#FBF3DC",
        layers=(
            _DemoLayer(
                text="// thesaurus.next()",
                font_family="VT323",
                text_color="#7A6A4E",
                box=_b(0.1, 0.15, 0.8, 0.1),
                font_size_pct=4.0,
                text_align="left",
            ),
            _DemoLayer(
                # 16% of box width keeps "Liberate" on one line at the
                # \n break. v1's 30% (height-based) → ~324px font; B3
                # made it width-based which would be ~518px and wraps.
                text="Liberate\nyour sign.",
                font_family="Playfair Display",
                text_color="#1A1610",
                box=_b(0.05, 0.27, 0.9, 0.6),
                font_size_pct=16.0,
            ),
        ),
        transition_out="push",
        transition_ms=600,
    ),

    # 6 · UNCAGE — Alfa Slab on amber→scarlet gradient, shaking.
    _DemoFrame(
        name="06 · Uncage!!",
        duration_ms=1700,
        background_pattern=BackgroundPattern(
            pattern="gradient",
            color_a="#FFB43C",
            color_b="#5E1A1A",
            density=0.0,  # 0deg = top→bottom
        ),
        layers=(
            _DemoLayer(
                # Cream-yellow on amber→scarlet so the type pops against
                # both gradient stops -- prior #1A0F00 disappeared on the
                # dark-red bottom (B20, 2026-05-05).
                text="UNCAGE\nYOUR SIGN!!",
                font_family="Alfa Slab One",
                text_color="#FFF1B0",
                box=_b(0.05, 0.15, 0.9, 0.65),
                # Bug 4 (2026-05-19): bumped from 33 → 100 so every wrapped
                # line naturally overflows boxW and triggers the per-line
                # X-squish-to-boxW. Achieves "each line fills boxW edge-to-
                # edge" intent via content, not renderer stretch-up.
                font_size_pct=100.0,
                motion="shake",
                motion_intensity=70,
            ),
            _DemoLayer(
                # Same B20 fix -- soft amber-cream caption on the dark
                # gradient bottom; was #3A1A00 dark-brown-on-dark-red.
                text="// synonyms.length === 2",
                font_family="VT323",
                text_color="#FFD580",
                box=_b(0.1, 0.85, 0.8, 0.1),
                font_size_pct=4.0,
                text_align="left",
            ),
        ),
        transition_out="flip",
        transition_ms=600,
    ),

    # 7a · TYPO (broken) — confidently displayed mistake.
    _DemoFrame(
        name="07a · Typo (oops)",
        duration_ms=800,
        background_color="#050608",
        layers=(
            _DemoLayer(
                text="FREE YOUR SGIN!!",
                font_family="Bowlby One SC",
                text_color="#FFFFFF",
                box=_b(0.05, 0.25, 0.9, 0.45),
                font_size_pct=14.0,
            ),
            _DemoLayer(
                text="// it's your sign.",
                font_family="VT323",
                text_color="#FF5A6B",
                box=_b(0.1, 0.78, 0.8, 0.1),
                font_size_pct=5.0,
                text_align="left",
            ),
        ),
        transition_out="cut",  # internal cut to mid-fix
        transition_ms=500,
    ),

    # 7b · TYPO (mid-fix) — striking through SG, glow on insertion.
    _DemoFrame(
        name="07b · Typo (mid-fix)",
        duration_ms=350,
        background_color="#050608",
        layers=(
            _DemoLayer(
                text="FREE YOUR S?IN!!",  # placeholder while letters swap
                font_family="Bowlby One SC",
                text_color="#FFFFFF",
                box=_b(0.05, 0.25, 0.9, 0.45),
                font_size_pct=14.0,
                motion="pulse",
                motion_intensity=80,
                motion_speed=1.4,
            ),
            _DemoLayer(
                text="// fixing...",
                font_family="VT323",
                text_color="#5AF095",
                box=_b(0.1, 0.78, 0.8, 0.1),
                font_size_pct=5.0,
                text_align="left",
            ),
        ),
        transition_out="cut",  # internal cut to fixed
        transition_ms=500,
    ),

    # 7c · TYPO (fixed) — clean, with green confirmation caption.
    _DemoFrame(
        name="07c · Typo (fixed)",
        duration_ms=1200,
        background_color="#050608",
        layers=(
            _DemoLayer(
                text="FREE YOUR SIGN.",
                font_family="Bowlby One SC",
                text_color="#FFFFFF",
                box=_b(0.05, 0.25, 0.9, 0.45),
                font_size_pct=14.0,
            ),
            _DemoLayer(
                text="// fixed.",
                font_family="VT323",
                text_color="#5AF095",
                box=_b(0.1, 0.78, 0.8, 0.1),
                font_size_pct=5.0,
                text_align="left",
            ),
        ),
        transition_out="shutter",
        transition_ms=600,
    ),

    # 8 · TILE CHAOS — 5 overlapping center-crossing layers, each with a
    # different font/color/motion/phase. Boxes are ~0.5×0.5 with off-grid
    # positions so they cross the center and read as chaos, not as a
    # clean 2×2 grid (qarl 2026-05-04 rebuild ask).
    _DemoFrame(
        name="08 · Tile Chaos",
        duration_ms=1500,
        background_color="#050608",
        layers=(
            # Top-left, slightly offset down-right
            _DemoLayer(
                text="FREE YOUR\nSIGN!!!",
                font_family="Bowlby One SC",
                text_color="#FFB43C",
                box=_b(0.02, 0.04, 0.5, 0.5),
                font_size_pct=22.0,
                motion="shake",
                motion_intensity=80,
                motion_phase=0.0,
            ),
            # Top-right, offset slightly down
            _DemoLayer(
                text="FREE YOUR\nSIGN!!!",
                font_family="Anton",
                text_color="#5AF095",
                box=_b(0.48, 0.10, 0.5, 0.5),
                font_size_pct=24.0,
                motion="bounce",
                motion_intensity=70,
                motion_phase=0.25,
            ),
            # Bottom-left, slightly offset right
            _DemoLayer(
                text="FREE YOUR\nSIGN!!!",
                font_family="Permanent Marker",
                text_color="#5FD5FF",
                box=_b(0.08, 0.48, 0.5, 0.5),
                font_size_pct=22.0,
                motion="pulse",
                motion_intensity=80,
                motion_phase=0.5,
            ),
            # Bottom-right, slight offset down
            _DemoLayer(
                text="FREE YOUR\nSIGN!!!",
                font_family="Caveat Brush",
                text_color="#FFB43C",
                box=_b(0.46, 0.52, 0.5, 0.48),
                font_size_pct=24.0,
                motion="blink",
                motion_intensity=70,
                motion_phase=0.7,
            ),
            # Bonus 5th layer — mono, white, blinking fast, half-opacity
            # so it reads as a glitch overlay rather than competing
            # with the four corners. qarl 2026-05-06 round-2: enlarged
            # + repositioned up-left so it dominates more of the frame.
            _DemoLayer(
                text="FREE YOUR\nSIGN!!!",
                font_family="JetBrains Mono",
                text_color="#FFFFFF",
                box=_b(0.1599, 0.1667, 0.6711, 0.7013),
                font_size_pct=24.5,
                motion="blink",
                motion_intensity=90,
                motion_phase=0.4,
                motion_speed=1.6,
                opacity=0.55,
            ),
        ),
        transition_out="pixelate",
        transition_ms=600,
    ),

    # 9 · CHANT WALL — 5 ticker stripes. qarl 2026-05-06 editor pass:
    # duration 1700→2000ms; layers re-stacked with overlapping boxes
    # for a denser look + reduced font sizes per stripe.
    _DemoFrame(
        name="09 · Chant Wall",
        duration_ms=2000,
        background_color="#050608",
        layers=(
            _DemoLayer(
                text=_CHANT_TEXT, font_family="Bowlby One SC",
                text_color="#FFB43C",
                box=_b(0.048, 0.1261, 0.9, 0.16),
                font_size_pct=8.0,
                motion="ticker", motion_intensity=70, motion_phase=0.0,
            ),
            _DemoLayer(
                text=_CHANT_TEXT, font_family="Anton",
                text_color="#5AF095",
                box=_b(0.0658, 0.0727, 0.9, 0.5574),
                font_size_pct=15.0,
                motion="ticker", motion_intensity=60, motion_phase=0.5,
            ),
            _DemoLayer(
                text=_CHANT_TEXT, font_family="Alfa Slab One",
                text_color="#FF5FA7",
                box=_b(0.0646, 0.3489, 0.9, 0.3293),
                font_size_pct=8.5,
                motion="ticker", motion_intensity=70, motion_phase=0.2,
            ),
            _DemoLayer(
                # qarl edit landed with one trailing space dropped on
                # this layer; preserved as-is to match Pi state.
                text="FREE  YOUR  SIGN ",
                font_family="Permanent Marker",
                text_color="#5FD5FF",
                box=_b(0.0529, 0.4715, 0.9, 0.2852),
                font_size_pct=10.0,
                motion="ticker", motion_intensity=60, motion_phase=0.8,
            ),
            _DemoLayer(
                # qarl edit landed with no trailing space on this layer.
                text="FREE  YOUR  SIGN",
                font_family="Caveat Brush",
                text_color="#FFFFFF",
                box=_b(0.05, 0.5851, 0.9, 0.272),
                font_size_pct=12.5,
                motion="ticker", motion_intensity=80, motion_phase=0.3,
            ),
        ),
        transition_out="marquee",
        transition_ms=600,
    ),

    # 10 · SCREAM — Anton ticker on rainbow gradient. qarl 2026-05-06
    # editor pass: duration 600→1000ms, ticker font size 40→11.5,
    # box stretched to fill more of the slide vertically.
    _DemoFrame(
        name="10 · Scream",
        duration_ms=1000,
        background_pattern=BackgroundPattern(
            pattern="gradient",
            color_a="#FF5FA7",
            color_b="#5AF095",
            density=0.0,
        ),
        layers=(
            _DemoLayer(
                text=_SCREAM_TEXT,
                font_family="Anton",
                text_color="#050608",
                box=_b(0.05, 0.0817, 0.9, 0.8151),
                font_size_pct=11.5,
                motion="ticker",
                motion_intensity=85,
            ),
        ),
        transition_out="cut",
        transition_ms=500,
    ),

    # 11 · SILENCE — // signal lost, dying carrier on slow pulse.
    # motion_speed=0.5 for an ominous half-tempo heartbeat (B2).
    _DemoFrame(
        name="11 · Silence",
        duration_ms=1100,
        background_color="#000000",
        layers=(
            _DemoLayer(
                text="// signal lost",
                font_family="VT323",
                text_color="#7A4318",
                box=_b(0.1, 0.4, 0.8, 0.2),
                font_size_pct=10.0,
                motion="pulse",
                motion_intensity=80,
                motion_speed=0.5,
                text_align="left",
            ),
        ),
        transition_out="scanline",
        transition_ms=600,
    ),

    # 12 · STADIUM — typed line + ticker echo. Echo at 0.4 opacity for
    # ghost-trail feel; ticker at 1.4x speed for urgency.
    _DemoFrame(
        name="12 · Stadium",
        duration_ms=2100,
        background_color="#050608",
        layers=(
            _DemoLayer(
                text="> chant.start()",
                font_family="JetBrains Mono",
                text_color="#7A5418",
                box=_b(0.1, 0.18, 0.8, 0.1),
                font_size_pct=5.0,
                text_align="left",
            ),
            _DemoLayer(
                # qarl 2026-05-06 round-2: dialed back to 8% with a
                # small box nudge for tighter terminal-line proportions.
                text="FREE YOUR SIGN_",
                font_family="JetBrains Mono",
                text_color="#FFB43C",
                box=_b(0.0555, 0.3298, 0.9, 0.3418),
                font_size_pct=8.0,
            ),
            _DemoLayer(
                # qarl 2026-05-06: shortened to single instance — ticker
                # repeats it visually as it scrolls.
                text="free your sign",
                font_family="Bowlby One SC",
                text_color="#FFFFFF",
                box=_b(0.05, 0.7, 0.9, 0.18),
                font_size_pct=8.0,
                motion="ticker",
                motion_intensity=60,
                motion_speed=1.4,
                opacity=0.4,
            ),
        ),
        transition_out="glitch",
        transition_ms=600,
    ),

    # 13a · PANIC FLASH (1/3) — Bowlby amber, shake max.
    _DemoFrame(
        name="13a · Panic 1",
        duration_ms=130,
        background_color="#050608",
        layers=(
            _DemoLayer(
                text="FREE YOUR SIGN!!!",
                font_family="Bowlby One SC",
                text_color="#FFB43C",
                box=_b(0.05, 0.2, 0.9, 0.6),
                font_size_pct=14.0,
                motion="shake",
                motion_intensity=100,
            ),
        ),
        transition_out="cut",
        transition_ms=500,
    ),

    # 13b · PANIC FLASH (2/3) — Alfa Slab pink, shake max.
    _DemoFrame(
        name="13b · Panic 2",
        duration_ms=130,
        background_color="#050608",
        layers=(
            _DemoLayer(
                text="FREE YOUR SIGN!!!",
                font_family="Alfa Slab One",
                text_color="#FF5FA7",
                box=_b(0.05, 0.2, 0.9, 0.6),
                font_size_pct=13.0,
                motion="shake",
                motion_intensity=100,
            ),
        ),
        transition_out="cut",
        transition_ms=500,
    ),

    # 13c · PANIC FLASH (3/3) — Permanent Marker cyan, shake max.
    # Slightly longer hold so the iris-out has air to breathe.
    _DemoFrame(
        name="13c · Panic 3",
        duration_ms=500,
        background_color="#050608",
        layers=(
            _DemoLayer(
                text="FREE YOUR SIGN!!!",
                font_family="Permanent Marker",
                text_color="#5FD5FF",
                box=_b(0.05, 0.2, 0.9, 0.6),
                font_size_pct=13.0,
                motion="shake",
                motion_intensity=100,
                motion_speed=1.2,
            ),
        ),
        transition_out="iris",
        transition_ms=600,
    ),

    # 14 · COOLDOWN — credits, slow blink "// loop · 1/∞" at 0.7
    # opacity for a fading-out vibe.
    _DemoFrame(
        name="14 · Cooldown",
        duration_ms=2400,
        background_color="#050608",
        layers=(
            _DemoLayer(
                text="// openMarquee",
                font_family="VT323",
                text_color="#7A5418",
                box=_b(0.1, 0.18, 0.8, 0.1),
                font_size_pct=7.0,
                text_align="left",
            ),
            _DemoLayer(
                # qarl 2026-05-06: 24 → 14 for tighter cooldown headline.
                text="FREE YOUR\nSIGN.",
                font_family="Bowlby One SC",
                text_color="#FFB43C",
                box=_b(0.05, 0.30, 0.9, 0.45),
                font_size_pct=14.0,
            ),
            _DemoLayer(
                text="// loop · 1/∞",
                font_family="VT323",
                text_color="#A87830",
                box=_b(0.1, 0.83, 0.8, 0.1),
                font_size_pct=6.0,
                motion="blink",
                motion_intensity=20,
                motion_speed=0.5,
                opacity=0.7,
                text_align="left",
            ),
        ),
        transition_out="scroll",  # to BOOT
        transition_ms=600,
    ),

    # 15 · BOOT — VT323 boot log + breathing status badge. Loop
    # transition Boot→FREE uses iris (qarl 2026-05-04: "iris was
    # the original Boot→FREE choice and still works").
    _DemoFrame(
        name="15 · Boot",
        duration_ms=2000,
        background_color="#050608",
        layers=(
            _DemoLayer(
                # 5% of box width fits each boot-log line on one line.
                # v1's 10% (height-based) was ~108px; B3 width-based at
                # 10% would be ~173px and wraps the longest lines.
                text=_BOOT_LOG_TEXT,
                font_family="VT323",
                text_color="#FFB43C",
                box=_b(0.05, 0.18, 0.9, 0.64),
                font_size_pct=5.0,
                text_align="left",
            ),
            _DemoLayer(
                text="● PANEL-0 OK",
                font_family="VT323",
                text_color="#FFB43C",
                box=_b(0.55, 0.05, 0.4, 0.1),
                font_size_pct=12.0,
                motion="breathe",
                motion_intensity=45,
                motion_speed=0.7,  # slow heartbeat (B2)
            ),
        ),
        # qarl 2026-05-06: loop transition Boot→FREE switched iris→scroll.
        transition_out="scroll",
        transition_ms=600,
    ),
)


_DEMO_REEL_PNG_DIR = Path(__file__).parent / "seed_assets" / "demo_reel"


def _seed_demo_reel_slides(
    storage: ContentStorage,
    width: int,
    height: int,
) -> list[TextSlide]:
    """Save the FREE YOUR SIGN demo reel as a list of TextSlides, loading
    pre-baked PNGs from `seed_assets/demo_reel/`.

    Each frame becomes one TextSlide with multi-layer text + per-layer
    motion + an optional background_pattern. The slides preserve the
    `transition_out` choice on the spec — the caller threads that into
    the playlist's per-item transition.

    qarl-direct 2026-05-13 (DELETE-PIL phase 1): pre-baked PNGs live
    next to seed.py; re-bake any time `_DEMO_REEL` changes via
    `scripts/prebake_seed_assets.py`. Pre-bakes are 1920×1080 — the
    device's playback engine cover-fits when rendering at the panel's
    native dims, so the same asset works for HUB75 64×32 / HDMI 1080p
    / composite 720×480. `width` and `height` params are kept on the
    signature for back-compat but no longer drive rasterization.
    """
    slides: list[TextSlide] = []
    for slot_idx, spec in enumerate(_DEMO_REEL):
        png_path = _DEMO_REEL_PNG_DIR / f"{slot_idx:02d}.png"
        png = png_path.read_bytes()
        # Build the TextSlide record. Per-layer motion + box + blend +
        # font + color all carry forward; the device's playback engine
        # re-composes per-tick at playback time using these.
        text_layers = [
            TextLayer(
                text=layer.text,
                font_family=layer.font_family,
                text_color=layer.text_color,
                font_size_pct=layer.font_size_pct,
                font_size_px=layer.font_size_px,
                box=TextBox(
                    x=layer.box[0], y=layer.box[1],
                    w=layer.box[2], h=layer.box[3],
                ),
                motion=layer.motion,
                motion_intensity=layer.motion_intensity,
                motion_phase=layer.motion_phase,
                motion_speed=layer.motion_speed,
                blend=layer.blend,
                opacity=layer.opacity,
                text_align=layer.text_align,
            )
            for layer in spec.layers
        ]
        slide = TextSlide(
            name=spec.name,
            background_color=spec.background_color,
            background_pattern=spec.background_pattern,
            duration_ms=spec.duration_ms,
            text_layers=text_layers,
        )
        storage.save_text_slide(slide, png)
        slides.append(slide)
    return slides


def render_welcome_png(width: int, height: int) -> bytes:
    """PNG for the first 'Welcome' slide at the panel's native dims. Kept
    as a thin shim for tests + historical callers; new code should prefer
    render_text_slide_png()."""
    return render_text_slide_png("Welcome", width, height)


def _wrap_text_to_width(
    text: str, draw, font, emoji_font, max_width: int,
) -> str:
    """Insert \\n at word boundaries so each line fits within
    `max_width`. Preserves any pre-existing literal \\n breaks.
    Single words wider than the box are left intact (the existing
    horizontal-squish path will handle overflow).

    Used by _draw_text_into for B4 word-wrap (2026-05-05) so prose
    that exceeds the box width flows onto multiple lines instead of
    requiring squish or visual clipping."""
    if not text or max_width <= 0:
        return text
    out_lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            out_lines.append("")
            continue
        words = paragraph.split(" ")
        line: list[str] = []
        for word in words:
            if not line:
                line.append(word)
                continue
            test_line = " ".join(line + [word])
            bbox = _measure_text_runs(draw, test_line, font, emoji_font)
            if (bbox[2] - bbox[0]) > max_width:
                out_lines.append(" ".join(line))
                line = [word]
            else:
                line.append(word)
        if line:
            out_lines.append(" ".join(line))
    return "\n".join(out_lines)


def _draw_text_into(
    img: Image.Image,
    *,
    text: str,
    fg: str,
    font_family: str | None,
    box: object | None,
    slide_width: int,
    slide_height: int,
    font_size_pct: float | None = None,
    font_size_px: int | None = None,
    text_align: str = "center",
    outline_color: str | None = None,
) -> None:
    """Compose one text layer onto `img` in place.

    Shared by render_text_slide_png (single-layer rendering, legacy
    callers + text editor seed path) and render_layered_text_slide_png
    (the v3 multi-layer compositor). Centers the text inside `box`.

    Font sizing (qarl 2026-05-01 review #3, refining ask #1):
    font_size_pct is a percentage of BOX WIDTH — resizing the box
    visibly resizes the text. (Earlier same day went % of slide width;
    qarl's review correction pinned it to box width because operators
    expected box-resize → text-resize.) Squish (ask #1): if the
    rendered text overflows the box on EITHER axis, Lanczos-resize
    that axis to fit. Vertical squish handles multi-line text that
    exceeds box height; horizontal squish stays as a fallback for
    verbose single-line text. Both axes squish independently when
    both overflow.

    Outline (2026-05-02, motion unification): when `outline_color`
    is set, the glyphs render with a 1-px stroke in that color. The
    auto-mode renderer (compose_auto_frame) used to bake a black
    1-px halo for readability on mid-tone backgrounds; the unified
    motion path preserves that behavior by setting outline_color
    on auto-mode layers from render_layer_to_rgba.
    """
    if not text:
        return
    if box is None:
        bx, by, bw, bh = 0.0, 0.0, 1.0, 1.0
    else:
        bx = float(getattr(box, "x", 0.0))
        by = float(getattr(box, "y", 0.0))
        bw = float(getattr(box, "w", 1.0))
        bh = float(getattr(box, "h", 1.0))
    px_box_x = int(bx * slide_width)
    px_box_y = int(by * slide_height)
    px_box_w = max(1, int(bw * slide_width))
    px_box_h = max(1, int(bh * slide_height))

    draw = ImageDraw.Draw(img)
    if font_size_px is not None and font_size_px > 0:
        size_px = int(font_size_px)
    elif font_size_pct is not None and font_size_pct > 0:
        # §5.10a v3.1.2 (qarl 2026-05-01 review #3): pct is of BOX
        # width. Resizing the box visibly resizes the text.
        size_px = max(4, int(px_box_w * font_size_pct / 100.0))
    else:
        size_px = max(12, int(px_box_w * 0.3))
    font = _load_text_font(font_family, size_px)
    # Emoji font is None unless the operator dropped Noto Color Emoji
    # at ui/fonts/noto-color-emoji.ttf. When present, mixed-content
    # strings render in segmented runs; absent, emoji codepoints fall
    # through to the regular font (showing .notdef tofu but not
    # crashing).
    emoji_font = _load_emoji_font(size_px)
    # B4 (2026-05-05): word-wrap to box width before measurement so
    # prose flows onto multiple lines instead of requiring squish or
    # clipping. Pre-existing literal \n breaks are preserved.
    text = _wrap_text_to_width(text, draw, font, emoji_font, px_box_w)
    bbox = _measure_text_runs(draw, text, font, emoji_font)
    natural_w = bbox[2] - bbox[0]
    natural_h = bbox[3] - bbox[1]
    box_center_x = px_box_x + px_box_w / 2
    box_center_y = px_box_y + px_box_h / 2
    if natural_w <= 0 or natural_h <= 0:
        return
    # Pillow's stroke_width / stroke_fill kwargs add an N-pixel halo in
    # the stroke color around each glyph BEFORE the foreground fill is
    # applied. Used by auto-mode layers for the readability outline
    # the prior compose_auto_frame baked in.
    stroke_kwargs = (
        {"stroke_width": 1, "stroke_fill": outline_color}
        if outline_color
        else {}
    )

    # Fits inside the box on both axes — paint directly, no squish.
    if natural_w <= px_box_w and natural_h <= px_box_h:
        # Anchor the bbox to the box's left/center/right edge per
        # text_align so single-line short labels align to the box
        # itself, not to the bbox center. Multi-line per-line
        # alignment within the bbox happens via PIL's align kwarg
        # in _draw_text_runs.
        if text_align == "left":
            anchor_x = px_box_x - bbox[0]
        elif text_align == "right":
            anchor_x = px_box_x + px_box_w - natural_w - bbox[0]
        else:
            anchor_x = box_center_x - natural_w / 2 - bbox[0]
        _draw_text_runs(
            draw,
            (anchor_x, box_center_y - natural_h / 2 - bbox[1]),
            text,
            fg=fg, font=font, emoji_font=emoji_font,
            stroke_kwargs=stroke_kwargs,
            align=text_align,
        )
        return
    # Squish: render at natural size on a transparent surface, then
    # resize whichever axes overflow the box. Lanczos preserves glyph
    # legibility at small scale factors. The pad trick survives the
    # resize because pad-pre is scaled inversely to keep pad-post a
    # constant pixel count after the squeeze.
    pad_post = 4
    target_w = min(natural_w, px_box_w)
    target_h = min(natural_h, px_box_h)
    ratio_w = target_w / max(1, natural_w)
    ratio_h = target_h / max(1, natural_h)
    pad_pre_w = max(pad_post, math.ceil(pad_post / max(ratio_w, 0.001)))
    pad_pre_h = max(pad_post, math.ceil(pad_post / max(ratio_h, 0.001)))
    temp = Image.new(
        "RGBA",
        (natural_w + pad_pre_w * 2, natural_h + pad_pre_h * 2),
        (0, 0, 0, 0),
    )
    td = ImageDraw.Draw(temp)
    _draw_text_runs(
        td,
        (pad_pre_w - bbox[0], pad_pre_h - bbox[1]),
        text,
        fg=fg, font=font, emoji_font=emoji_font,
        stroke_kwargs=stroke_kwargs,
        align=text_align,
    )
    squished = temp.resize(
        (target_w + pad_post * 2, target_h + pad_post * 2), Image.LANCZOS
    )
    paste_x = int(box_center_x - squished.width / 2)
    paste_y = int(box_center_y - squished.height / 2)
    img.paste(squished, (paste_x, paste_y), squished)


def render_layered_text_slide_png(
    layers: list,
    width: int,
    height: int,
    bg: str = WELCOME_BG_COLOR,
    *,
    background_image_path: Path | None = None,
) -> bytes:
    """Composite a list of TextLayers onto a single PNG (§5.10a v3).

    Layers render in array order: index 0 first, later entries paint
    on top. Each layer's `box` positions+clips its text; font_family
    + text_color come per-layer. Background (solid fill or cover-fit
    image) is shared across all layers — slide-level per the spec.
    """
    if background_image_path is not None and background_image_path.exists():
        with Image.open(background_image_path) as src:
            img = _cover_fit(src.convert("RGB"), width, height)
    else:
        img = Image.new("RGB", (width, height), bg)
    for layer in layers:
        _draw_text_into(
            img,
            text=getattr(layer, "text", ""),
            fg=getattr(layer, "text_color", WELCOME_TEXT_COLOR),
            font_family=getattr(layer, "font_family", None),
            box=getattr(layer, "box", None),
            slide_width=width,
            slide_height=height,
            font_size_pct=getattr(layer, "font_size_pct", None),
            font_size_px=getattr(layer, "font_size_px", None),
            text_align=getattr(layer, "text_align", "center") or "center",
        )
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_text_slide_png(
    text: str,
    width: int,
    height: int,
    fg: str = WELCOME_TEXT_COLOR,
    bg: str = WELCOME_BG_COLOR,
    *,
    background_image_path: Path | None = None,
    font_family: str | None = None,
    box: object | None = None,
    font_size_pct: float | None = None,
    font_size_px: int | None = None,
) -> bytes:
    """Flatten a single text+box onto a PNG.

    Single-layer rendering -- used by the test_rerender single-text
    fixtures + render_welcome_png. The layered compositor is
    `render_layered_text_slide_png` (§5.10a v3); both share the
    `_draw_text_into` helper so squish + box-centering stay consistent.
    """
    if background_image_path is not None and background_image_path.exists():
        with Image.open(background_image_path) as src:
            img = _cover_fit(src.convert("RGB"), width, height)
    else:
        img = Image.new("RGB", (width, height), bg)

    _draw_text_into(
        img,
        text=text,
        fg=fg,
        font_family=font_family,
        box=box,
        slide_width=width,
        slide_height=height,
        font_size_pct=font_size_pct,
        font_size_px=font_size_px,
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


# Codepoint ranges that render via the emoji font when one is bundled.
# Per the post-arc spec from QA: U+1F000..U+1FFFF (Mahjong / supplementary
# plane emoji) and U+2600..U+27BF (misc symbols + dingbats). Refining
# beyond these ranges (ZWJ sequences, regional indicator pairs for
# flags, skin-tone modifiers) is out of scope for v1 — those split into
# multiple runs but each individual codepoint still finds an emoji
# glyph in Noto Color Emoji.
_EMOJI_CODEPOINT_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1FFFF),
    (0x2600, 0x27BF),
)


def _is_emoji_codepoint(codepoint: int) -> bool:
    """True if `codepoint` falls in any range that should render with
    the bundled emoji font instead of the layer's regular font."""
    return any(lo <= codepoint <= hi for lo, hi in _EMOJI_CODEPOINT_RANGES)


def _segment_text_for_emoji(text: str) -> list[tuple[str, str]]:
    """Split `text` into adjacent same-kind runs.

    Returns a list of (kind, substring) tuples where kind is "text"
    (render with the layer's font) or "emoji" (render with the
    bundled emoji font, embedded_color=True). Adjacent codepoints of
    the same kind merge into one run so each draw.text call is as
    chunky as possible — fewer cursor-advance roundtrips, simpler
    measurement.

    Empty input returns []. A pure-text string returns one ("text",
    full_text) run; a pure-emoji string returns one ("emoji",
    full_text) run.
    """
    if not text:
        return []
    runs: list[tuple[str, str]] = []
    cur_kind = "emoji" if _is_emoji_codepoint(ord(text[0])) else "text"
    cur_buf = [text[0]]
    for ch in text[1:]:
        kind = "emoji" if _is_emoji_codepoint(ord(ch)) else "text"
        if kind == cur_kind:
            cur_buf.append(ch)
        else:
            runs.append((cur_kind, "".join(cur_buf)))
            cur_kind = kind
            cur_buf = [ch]
    runs.append((cur_kind, "".join(cur_buf)))
    return runs


def _load_emoji_font(size_px: int):
    """Load Noto Color Emoji at size_px from ui/fonts/, or None.

    Operators wanting emoji on the device drop noto-color-emoji.ttf
    (free download from Google Fonts) at ui/fonts/. Until they do,
    this returns None and emoji codepoints render via the layer's
    regular font (which on most fonts shows .notdef tofu boxes —
    not great, but doesn't crash). Bundling the ~10 MB color-emoji
    TTF in the repo is intentionally deferred to a future install
    script (scripts/install/download-emoji-font.sh, TBD).
    """
    try:
        from openmarquee.text_raster import bundled_fonts_dir, cached_truetype
        path = bundled_fonts_dir() / "noto-color-emoji.ttf"
        if not path.exists():
            return None
        # Noto Color Emoji is a CBDT bitmap font; PIL picks the closest
        # bitmap strike. Pass any size the layer wants — Pillow scales.
        return cached_truetype(str(path), size_px)
    except (OSError, Exception):
        return None


def _measure_text_runs(draw, text: str, font, emoji_font) -> tuple[int, int, int, int]:
    """Return a (x0, y0, x1, y1) bbox-shaped tuple measuring `text`
    when rendered as emoji-segmented runs. Equivalent to
    draw.textbbox when there's no emoji or no emoji_font available.

    The runs are placed contiguously left-to-right with the cursor
    advancing by each run's draw.textbbox width; vertical extent is
    the union of all runs' bboxes. This matches the layout done by
    _draw_text_runs so the natural-size measurement used for box-
    fit / squish decisions stays accurate when emoji is present."""
    if emoji_font is None:
        return draw.textbbox((0, 0), text, font=font)
    runs = _segment_text_for_emoji(text)
    if all(kind == "text" for kind, _ in runs):
        return draw.textbbox((0, 0), text, font=font)
    cursor_x = 0.0
    x0_first: int | None = None
    min_y = 0
    max_y = 0
    for kind, run_text in runs:
        run_font = emoji_font if kind == "emoji" else font
        bbox = draw.textbbox((int(cursor_x), 0), run_text, font=run_font)
        if x0_first is None:
            x0_first = bbox[0]
        if bbox[1] < min_y:
            min_y = bbox[1]
        if bbox[3] > max_y:
            max_y = bbox[3]
        # Advance by the run's horizontal advance width (font.getlength)
        # not the ink-bbox right edge (bbox[2]). Glyphs with right-side
        # bearing — common at text↔emoji run boundaries — would
        # otherwise visually touch or overlap by a pixel.
        cursor_x += run_font.getlength(run_text)
    return (x0_first or 0, min_y, int(round(cursor_x)), max_y)


def _draw_text_runs(
    draw, xy: tuple[float, float], text: str, *,
    fg: str, font, emoji_font, stroke_kwargs: dict,
    align: str = "center",
) -> None:
    """Render `text` at `xy` using emoji_font for emoji codepoints
    and `font` for the rest. Falls back to a single draw.text call
    when emoji_font is None or text has no emoji.

    `align` controls per-line horizontal alignment within the text's
    natural bbox for multi-line input ("left"/"center"/"right").
    Emoji-segmented text uses center alignment regardless (B4 limit,
    2026-05-05 -- align with emoji is a follow-up if it ever shows
    up in practice)."""
    pil_align = align if align in ("left", "center", "right") else "center"
    if emoji_font is None:
        draw.text(
            xy, text, fill=fg, font=font, align=pil_align, **stroke_kwargs,
        )
        return
    runs = _segment_text_for_emoji(text)
    if all(kind == "text" for kind, _ in runs):
        draw.text(
            xy, text, fill=fg, font=font, align=pil_align, **stroke_kwargs,
        )
        return
    cursor_x = float(xy[0])
    cursor_y = xy[1]
    for kind, run_text in runs:
        run_font = emoji_font if kind == "emoji" else font
        if kind == "emoji":
            # embedded_color=True renders the CBDT/COLR color glyphs.
            # fill / stroke don't apply to color glyphs (the colors
            # are baked into the font's bitmap data), so we omit them.
            draw.text(
                (cursor_x, cursor_y), run_text,
                font=run_font, embedded_color=True,
            )
        else:
            draw.text(
                (cursor_x, cursor_y), run_text,
                fill=fg, font=run_font, **stroke_kwargs,
            )
        # Advance by horizontal-advance width (not ink-bbox right edge)
        # so consecutive runs in different fonts don't visually touch
        # at glyph boundaries.
        cursor_x += run_font.getlength(run_text)


def _load_text_font(family: str | None, size_px: int):
    """Load a bundled @font-face TTF when `family` matches an entry in
    `text_raster.BUNDLED_FONT_FILES`; otherwise the historical bold
    fallback (DejaVuSans-Bold → Arial Bold → bitmap)."""
    from openmarquee.text_raster import (
        BUNDLED_FONT_FILES,
        bundled_fonts_dir,
        cached_truetype,
    )
    if family:
        bundled_name = BUNDLED_FONT_FILES.get(family)
        if bundled_name:
            path = bundled_fonts_dir() / bundled_name
            if path.exists():
                try:
                    return cached_truetype(str(path), size_px)
                except OSError:
                    pass
    try:
        return cached_truetype("DejaVuSans-Bold.ttf", size_px)
    except OSError:
        try:
            return cached_truetype("Arial Bold.ttf", size_px)
        except OSError:
            return ImageFont.load_default()


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
    # 11.2: atomic_write_text sets 0600 + cleans up orphan .tmp.
    atomic_write_text(path, json.dumps(payload, indent=2))
