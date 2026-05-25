"""Filesystem persistence for content items.

Layout under `root`:

    <id>/item.json   — metadata envelope (schema-versioned)
    <id>/asset.png   — the rendered asset (PNG for text slides)

Envelope format:

    {
        "schema_version": <int>,
        "updated_at": "<iso-8601 UTC>",   # added 2026-04: manifest timestamp
        "item": { ...serialized ContentItem... }
    }

The schema version lives on the envelope (not the model) so we can migrate
the on-disk format without churning the pure-data model. Writes are atomic
(write-to-temp + rename) so a crashed process can't leave a half-written
`item.json` that load() would choke on.

`updated_at` is what the flock manifest uses for last-writer-wins sync
decisions — it's on the envelope (not inside the item) because a peer
receiving our content preserves this stamp verbatim, while a local
edit bumps it.
"""

import json
import shutil
import threading
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID

from PIL import Image, ImageDraw, ImageFont
from pydantic import TypeAdapter

from openmarquee._atomic import atomic_write_bytes, atomic_write_text
from openmarquee.content import (
    ContentItem,
    ImageSlide,
    StreamSlide,
    TextSlide,
    VideoSlide,
    WebSlide,
)

# Bump when the on-disk envelope format changes in a non-backward-compatible
# way. load() will refuse to read older versions until a migration is written.
#
# v3 (2026-05-01): TextSlide gained `text_layers: list[TextLayer]` and the
#                  per-text fields (text / font_family / text_color /
#                  auto_mode / auto_format / box) moved off the slide root
#                  into the layer model. Clean cutover per qarl — dev
#                  envelopes need a one-time wipe.
# v2 (skipped): planned with the v1 single-box layout; never landed.
# v1: legacy (text fields at slide root, no box).
SCHEMA_VERSION = 3

_ENVELOPE_FILENAME = "item.json"
_ASSET_FILENAME = "asset.png"
_VIDEO_FILENAME = "asset.mp4"

# Pydantic adapter for the discriminated ContentItem union; routes to the
# right subclass on deserialize based on the `type` literal.
_CONTENT_ADAPTER: TypeAdapter[ContentItem] = TypeAdapter(ContentItem)

# The synthetic thumbnail card drawn for a StreamSlide (16:9).
_STREAM_PLACEHOLDER_SIZE = (640, 360)

# The synthetic placeholder card drawn for a WebSlide before its first
# screenshot arrives (16:9, same shape as the stream card).
_WEB_PLACEHOLDER_SIZE = (640, 360)


def _migrate_legacy_stream_item(item: dict) -> dict:
    """Remap a legacy `vlc_stream` item dict to the current `stream` shape.

    The `vlc_stream` slide type was renamed to `stream` (and its
    `rtsp_url` field to `stream_url`) — same data shape, just renamed
    identifiers, so this is NOT a SCHEMA_VERSION bump. `ContentItem` is
    a Pydantic discriminated union keyed on `type`: the discriminator
    runs *before* any model validator, so a `model_validator(mode=
    "before")` on the model would never see a `vlc_stream` envelope (it
    fails to match a union member first). The remap therefore has to
    happen on the raw parsed dict, before `_CONTENT_ADAPTER.validate_
    python`.

    Pure function: given the inner `item` dict, if its `type` is the
    legacy `"vlc_stream"` literal, return a *copy* with `type` set to
    `"stream"` and the `rtsp_url` key renamed to `stream_url` (only
    renamed when `stream_url` isn't already present, so a half-migrated
    hand-edited envelope doesn't lose data). Any other item is returned
    unchanged.

    P2 self-healing: this fixes the shape only in memory on load; the
    next save() rewrites item.json with the new literal, so the legacy
    form drains off disk as content gets edited.

    A malformed envelope whose `item` is not a dict at all (a list /
    str / None) is returned unchanged too: `.get` would raise on a
    non-dict, so the guard covers it and lets the downstream
    `_CONTENT_ADAPTER` produce the clean `ValidationError`.
    """
    if not isinstance(item, dict) or item.get("type") != "vlc_stream":
        return item
    migrated = dict(item)
    migrated["type"] = "stream"
    if "rtsp_url" in migrated and "stream_url" not in migrated:
        migrated["stream_url"] = migrated.pop("rtsp_url")
    return migrated


def _placeholder_font(size: int) -> ImageFont.ImageFont:
    """A scalable font for the stream placeholder card. Pillow's bundled
    default font is used at the requested size; the bare-default
    fallback covers any Pillow too old for the size argument."""
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    width: int,
    y: int,
    fill: tuple[int, int, int],
) -> None:
    """Draw `text` horizontally centred within `width` at vertical `y`."""
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    draw.text(((width - text_w) / 2, y), text, font=font, fill=fill)


def render_stream_placeholder_png(slide: StreamSlide) -> bytes:
    """Draw the synthetic 'stream' thumbnail card for a StreamSlide.

    A StreamSlide carries no operator-supplied image — the video is
    a live network feed. This card just gives the editor's saved-slides
    tile something to render; the device renderer never paints it.
    Per docs/STREAM_VLC_PROPOSAL.md §6 recommendation (b).
    """
    width, height = _STREAM_PLACEHOLDER_SIZE
    img = Image.new("RGB", (width, height), (24, 24, 28))
    draw = ImageDraw.Draw(img)

    # A play triangle inside an accent disc, slightly above centre.
    cx, cy, radius = width // 2, height // 2 - 24, 46
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=(255, 138, 0),
    )
    draw.polygon(
        [(cx - 14, cy - 22), (cx - 14, cy + 22), (cx + 24, cy)],
        fill=(24, 24, 28),
    )

    _draw_centered(
        draw,
        "Stream",
        _placeholder_font(30),
        width,
        cy + radius + 26,
        (235, 235, 235),
    )
    url = slide.stream_url
    if len(url) > 54:
        url = url[:51] + "…"
    _draw_centered(
        draw,
        url,
        _placeholder_font(18),
        width,
        cy + radius + 64,
        (150, 150, 156),
    )

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_web_placeholder_png(slide: WebSlide) -> bytes:
    """Draw the synthetic placeholder card for a WebSlide.

    A WebSlide's asset.png is a screenshot rendered on-device. Before
    the first screenshot arrives — and whenever a render fails — this
    card stands in so the slide tile (and the renderer) has something
    to show. The periodic-render producer overwrites it with a real
    screenshot.
    """
    width, height = _WEB_PLACEHOLDER_SIZE
    img = Image.new("RGB", (width, height), (24, 24, 28))
    draw = ImageDraw.Draw(img)

    # A globe-ish accent disc, slightly above centre.
    cx, cy, radius = width // 2, height // 2 - 24, 46
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=(0, 138, 255),
    )
    # Two meridians + an equator, suggesting a globe.
    draw.line((cx, cy - radius, cx, cy + radius), fill=(24, 24, 28), width=3)
    draw.line((cx - radius, cy, cx + radius, cy), fill=(24, 24, 28), width=3)
    draw.ellipse(
        (cx - radius // 2, cy - radius, cx + radius // 2, cy + radius),
        outline=(24, 24, 28),
        width=3,
    )

    _draw_centered(
        draw,
        "Web slide",
        _placeholder_font(30),
        width,
        cy + radius + 26,
        (235, 235, 235),
    )
    url = slide.url
    if len(url) > 54:
        url = url[:51] + "…"
    _draw_centered(
        draw,
        url,
        _placeholder_font(18),
        width,
        cy + radius + 64,
        (150, 150, 156),
    )

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class ContentStorage:
    """Persists content items as files on disk — one subdirectory per item."""

    # Perf counters (Batch 6.1). Class-level so the singleton's stats
    # survive across `Depends()` resolutions and any future per-test
    # instance creation. Snapshot via `GET /api/system/perf-stats`.
    _stats: dict[str, int] = {
        "list_all_calls": 0,
        "load_calls": 0,
        "save_calls": 0,
        "envelope_validates": 0,
    }

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # mtime-keyed in-memory cache (Batch 7.1). Skip the expensive
        # path -- json.loads + Pydantic discriminated-union validate --
        # when the envelope file's stat().st_mtime_ns matches what we
        # last cached. save()/delete() invalidate in lockstep so a
        # writer's change is visible on the next list_all().
        #
        # Instance-level (not class-level like `_stats`) because tests
        # use per-tmp_path ContentStorage and a class-level cache would
        # bleed across them. Relies on `Depends(get_content_storage)`
        # returning a singleton at runtime -- if that ever changes,
        # key on `(self.root, item_id)` instead.
        #
        # Cached items MUST be treated as read-only by callers --
        # mutation propagates to the next cache hit. (Verified clean
        # at write time -- grep for `\.text_layers =` / `\.name =` on
        # loaded items returns no mutation sites; document this here
        # so a future contributor adding one gets caught in review.)
        self._cache: dict[UUID, tuple[int, ContentItem]] = {}
        # Round-29 concurrency: serialize mutator methods (save /
        # save_video / delete). FastAPI sync handlers run on the
        # threadpool; two concurrent PUT /api/content/text-slides/
        # {X} requests (e.g. operator with two tabs open, both
        # autoSave-debounces firing within ~1 second) could otherwise
        # race in ContentStorage.save's two-file write
        # (envelope + asset). Pre-r29's _atomic.py also used a fixed
        # ".tmp" filename, so both writes shared the SAME staging
        # file -- byte interleaving in the staging buffer + last
        # rename wins with mashed bytes. Silent slide corruption;
        # operator sees 200 OK on both saves; renderer fails to
        # decode the asset at next playback.
        #
        # Part A of the r29 fix (this lock). Part B (per-call
        # ".tmp.<pid>.<hex>" suffix in _atomic.py) defends even when
        # a future caller forgets the lock.
        #
        # RLock (not Lock) because save_video acquires the lock at
        # its own level and then calls save() internally, which also
        # acquires -- threading.Lock would deadlock on re-entry from
        # the same thread; RLock is reentrant. Save sites that don't
        # re-enter (delete, save called directly) pay only a tiny
        # extra cost vs Lock for the reentry-tracking.
        #
        # Same caveat as r25/r27: the methods are sync and may be
        # called from async paths (flock_sync._ingest_update is
        # async). threading.RLock acquired from a coroutine briefly
        # blocks the event loop during contention; acceptable for
        # microsecond-scale ops (atomic_write is io-bound but short
        # per file); switching to asyncio.Lock would cascade
        # sync->async through every caller.
        self._lock = threading.RLock()

    @classmethod
    def stats_snapshot(cls) -> dict[str, int]:
        return dict(cls._stats)

    # --- writes ---

    def save(
        self,
        item: ContentItem,
        png: bytes,
        *,
        updated_at: datetime | None = None,
    ) -> None:
        """Persist any content item and its PNG asset. Overwrites if the id exists.

        Text slides and image slides both ship PNGs (the browser does the
        rendering/scaling and uploads bitmap pixel data in both cases). Video
        content, when it lands, will need a different asset extension —
        refactor the hardcoded `asset.png` then.

        `updated_at` defaults to `now()` — bump the local edit stamp.
        Pass an explicit value when ingesting from a peer so the
        original stamp travels with the content.
        """
        type(self)._stats["save_calls"] += 1
        # Round-29: lock-protect the envelope+asset write pair so two
        # concurrent saves to the same id can't interleave.
        with self._lock:
            item_dir = self.root / str(item.id)
            item_dir.mkdir(parents=True, exist_ok=True)

            stamp = updated_at or datetime.now(UTC)
            envelope = {
                "schema_version": SCHEMA_VERSION,
                "updated_at": stamp.isoformat(),
                # Exclude the model's updated_at mirror — the envelope is the
                # authoritative source. Two stamps for the same fact would
                # drift the moment someone hand-edits item.json.
                "item": item.model_dump(mode="json", exclude={"updated_at"}),
            }
            # Drop the cache entry BEFORE the disk write so an IO error
            # (atomic-rename failure on full disk) doesn't leave the cache
            # holding pre-save state that diverges from disk on retry.
            # Next load() repopulates with the canonical re-decoded shape.
            self._cache.pop(item.id, None)
            self._atomic_write_text(item_dir / _ENVELOPE_FILENAME, json.dumps(envelope, indent=2))
            self._atomic_write_bytes(item_dir / _ASSET_FILENAME, png)

    def save_text_slide(self, slide: TextSlide, png: bytes) -> None:
        """Persist a text slide — convenience wrapper for save()."""
        self.save(slide, png)

    def save_image(self, image: ImageSlide, png: bytes) -> None:
        """Persist an image — convenience wrapper for save()."""
        self.save(image, png)

    def save_video(
        self,
        video: VideoSlide,
        thumbnail_png: bytes,
        video_bytes: bytes,
        *,
        updated_at: datetime | None = None,
    ) -> None:
        """Persist a video: thumbnail PNG (for list views) + the MP4 bytes.

        Laid out next to each other under the item's dir so a future playback
        engine can grab `asset.mp4` directly (e.g. feed its path to ffmpeg)
        while the UI's list rendering keeps using the existing PNG endpoint.

        Transactional: if any of the three writes (envelope / thumbnail /
        mp4) fails, the whole item dir is torn down. Without this an
        envelope-only dir would show up in `list_all()` with a 404 on its
        video endpoint — the playback loop would cycle on it forever.

        `updated_at` semantics match save() — defaults to now() for local
        edits, accepts an explicit value so peer-ingest preserves the
        originating stamp.
        """
        # Round-29: lock-protect the THREE-file write trio (envelope
        # via save(), thumbnail via save(), video bytes via the
        # _atomic_write_bytes here). RLock allows save()'s inner
        # acquire on the same thread without deadlock. Without this,
        # a concurrent save_video to the same id could interleave its
        # video-bytes write with our envelope+png write -- partial
        # save_video state on disk (envelope/png from one operator,
        # video bytes from another) is unrenderable.
        with self._lock:
            item_dir = self.root / str(video.id)
            preexisting = item_dir.exists()
            try:
                self.save(video, thumbnail_png, updated_at=updated_at)
                self._atomic_write_bytes(item_dir / _VIDEO_FILENAME, video_bytes)
            except Exception:
                # Only rm if this save created the dir — don't blow away another
                # item if the id collision were hypothetical.
                if not preexisting and item_dir.exists():
                    shutil.rmtree(item_dir, ignore_errors=True)
                raise

    def save_stream(
        self,
        slide: StreamSlide,
        *,
        updated_at: datetime | None = None,
    ) -> None:
        """Persist a stream slide.

        Unlike the other slide types there is no operator-supplied
        asset — the video arrives live over the network. A synthetic
        'stream' thumbnail card is generated here and handed to save()
        as the asset.png, so the editor's saved-slides tile renders
        consistently with every other slide type (proposal §6).

        `updated_at` semantics match save() — defaults to now() for a
        local edit, accepts an explicit value so peer-ingest preserves
        the originating stamp.
        """
        self.save(
            slide,
            render_stream_placeholder_png(slide),
            updated_at=updated_at,
        )

    def save_web(
        self,
        slide: WebSlide,
        png_bytes: bytes | None = None,
        *,
        updated_at: datetime | None = None,
    ) -> None:
        """Persist a web slide.

        Unlike StreamSlide, a WebSlide DOES carry a stored asset.png:
        it is the screenshot the renderer paints (the Web slide is "an
        image slide whose asset.png is auto-refreshed from an on-device
        render").

        `png_bytes` is the screenshot to write. On create there is no
        screenshot yet — nothing has been rendered — so callers pass
        None and a synthetic placeholder card is generated and stored
        instead. The periodic-render producer calls this with real
        screenshot bytes to refresh the asset in place.

        `updated_at` semantics match save() — defaults to now() for a
        local edit, accepts an explicit value so peer-ingest preserves
        the originating stamp.
        """
        self.save(
            slide,
            png_bytes if png_bytes is not None else render_web_placeholder_png(slide),
            updated_at=updated_at,
        )

    def video_path(self, item_id: UUID) -> Path:
        """Filesystem path to an item's video payload (no IO)."""
        return self.root / str(item_id) / _VIDEO_FILENAME

    def read_video(self, item_id: UUID) -> bytes:
        """Read the MP4 payload. Raises FileNotFoundError if absent."""
        path = self.video_path(item_id)
        if not path.exists():
            raise FileNotFoundError(f"no video at {path}")
        return path.read_bytes()

    # --- reads ---

    def exists(self, item_id: UUID) -> bool:
        """True if a content item with this id has been persisted."""
        return (self.root / str(item_id) / _ENVELOPE_FILENAME).exists()

    def load(self, item_id: UUID) -> ContentItem:
        """Load a content item by id. Raises FileNotFoundError if missing."""
        type(self)._stats["load_calls"] += 1
        envelope_path = self.root / str(item_id) / _ENVELOPE_FILENAME
        if not envelope_path.exists():
            raise FileNotFoundError(f"no content item at {envelope_path}")

        # Batch 7.1: mtime-keyed cache. A `stat()` is ~microseconds
        # vs json.loads + discriminated-union validate at ~milliseconds
        # on a populated device. List-then-show-on-UI flows hit this
        # path 60-200x per playback session per the baseline data.
        mtime_ns = envelope_path.stat().st_mtime_ns
        cached = self._cache.get(item_id)
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]

        data = json.loads(envelope_path.read_text())
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"item {item_id} has schema_version {version}, "
                f"expected {SCHEMA_VERSION} — migration needed"
            )

        # Remap any legacy `vlc_stream` item to the current `stream`
        # shape BEFORE validation — the ContentItem union discriminates
        # on `type`, so the rename has to land on the raw dict (a
        # model_validator would never see an unmatched union member).
        data["item"] = _migrate_legacy_stream_item(data["item"])
        # TypeAdapter dispatches to the right ContentItem variant based on
        # the `type` literal. Unknown types surface as validation errors.
        type(self)._stats["envelope_validates"] += 1
        item = _CONTENT_ADAPTER.validate_python(data["item"])
        # Populate the output-only updated_at mirror so API consumers can
        # cachebust asset URLs (`?v=${updated_at}`) and pick up new bytes
        # after a re-render. Envelope is the authoritative source; falling
        # back to the file mtime mirrors read_updated_at's pre-flock path.
        stamp = data.get("updated_at")
        if stamp is not None:
            try:
                parsed = datetime.fromisoformat(stamp)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                item.updated_at = parsed
            except ValueError:
                # Malformed envelope stamp — leave the mirror null rather
                # than crash the load; the next save() rewrites the field.
                pass
        self._cache[item_id] = (mtime_ns, item)
        return item

    def read_asset(self, item_id: UUID) -> bytes:
        """Read the rendered asset bytes for a content item."""
        path = self.asset_path(item_id)
        if not path.exists():
            raise FileNotFoundError(f"no asset at {path}")
        return path.read_bytes()

    def asset_path(self, item_id: UUID) -> Path:
        """Return the filesystem path to an item's rendered asset (no IO)."""
        return self.root / str(item_id) / _ASSET_FILENAME

    def read_updated_at(self, item_id: UUID) -> datetime:
        """When this item was last written locally — used by the flock
        manifest for last-writer-wins sync decisions.

        Falls back to the envelope file's mtime for items saved before the
        updated_at field was added (pre-flock items on disk). New saves
        always write it explicitly.

        Returned datetime is always tz-aware (UTC) — a naive stamp from a
        hand-edited envelope or mismatched-code peer is coerced to UTC so
        downstream diff logic doesn't get a naive-vs-aware comparison.
        """
        envelope_path = self.root / str(item_id) / _ENVELOPE_FILENAME
        if not envelope_path.exists():
            raise FileNotFoundError(f"no content item at {envelope_path}")
        data = json.loads(envelope_path.read_text())
        # Mirror load()'s schema check so a future v2 envelope doesn't let
        # read_updated_at silently return stamps while load() would refuse.
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"item {item_id} has schema_version {version}, "
                f"expected {SCHEMA_VERSION} — migration needed"
            )
        stamp = data.get("updated_at")
        if stamp is None:
            return datetime.fromtimestamp(envelope_path.stat().st_mtime, tz=UTC)
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    def list_all(self) -> list[ContentItem]:
        """Return all persisted content items, sorted by id string.

        Resilient to the root being deleted at runtime (e.g. SD card swap,
        manual cleanup) — returns an empty list rather than raising.
        """
        type(self)._stats["list_all_calls"] += 1
        if not self.root.exists():
            return []
        items: list[ContentItem] = []
        for child in sorted(self.root.iterdir()):
            if not child.is_dir():
                continue
            try:
                item_id = UUID(child.name)
            except ValueError:
                continue  # skip non-UUID dirs (could be editor scratch, etc.)
            if not (child / _ENVELOPE_FILENAME).exists():
                continue
            items.append(self.load(item_id))
        return items

    def delete(self, item_id: UUID) -> None:
        """Delete a content item and everything in its directory.

        Uses shutil.rmtree so it's safe when the item grows into a subtree
        (e.g. HUB75 raw-frame sequences store many files under assets/).

        Round-29: lock-protected so a concurrent save() to the same
        id can't race (save creates the dir + writes files; delete
        rm-rfs the dir). Without the lock, save could land bytes in
        a dir that delete then wipes out partway through.
        """
        with self._lock:
            item_dir = self.root / str(item_id)
            if not item_dir.exists():
                raise FileNotFoundError(f"no content item at {item_dir}")
            shutil.rmtree(item_dir)
            self._cache.pop(item_id, None)

    # --- internals ---

    # 11.2: atomic-write text + bytes helpers moved to
    # openmarquee._atomic. The staticmethods are kept as thin
    # delegators so existing call sites + future subclasses keep
    # working unchanged; the helpers themselves now also set 0600
    # mode (sweep #5 #6: AP password / station password live in
    # adjacent files; consistent owner-only mode keeps the on-
    # disk surface narrow).
    _atomic_write_text = staticmethod(atomic_write_text)
    _atomic_write_bytes = staticmethod(atomic_write_bytes)
