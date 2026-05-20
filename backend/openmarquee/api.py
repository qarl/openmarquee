"""REST API for content items.

Today exposes just the text-slide path (the F&F demo's centerpiece).
Endpoints for image and video upload land alongside their respective
content variants, post-demo.
"""

import base64
import io
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field, ValidationError

from openmarquee.content import (
    BackgroundPattern,
    ContentItem,
    ImageSlide,
    TextBox,
    TextSlide,
    VideoSlide,
    VlcStreamSlide,
)
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    get_content_storage,
    get_flock_sync,
    get_playlist_storage,
    get_tombstone_storage,
)
from openmarquee.flock import FlockStorage
from openmarquee.flock_sync import FlockSync
from openmarquee.playlist import PlaylistStorage, list_in_playlist_order
from openmarquee.tombstone import TombstoneStorage

router = APIRouter(prefix="/api/content", tags=["content"])

StorageDep = Annotated[ContentStorage, Depends(get_content_storage)]
PlaylistDep = Annotated[PlaylistStorage, Depends(get_playlist_storage)]
TombstoneDep = Annotated[TombstoneStorage, Depends(get_tombstone_storage)]
FlockSyncDep = Annotated[FlockSync, Depends(get_flock_sync)]


# --- Batch 11.3 / sweep #5 #4: CORS allowlist helper ---
#
# The pre-Phase-A code stamped `Access-Control-Allow-Origin: *` on the
# 3 cross-flock-readable endpoints (/api/playback/current-{thumbnail,
# frame} and /api/system/info). A wildcard ACAO lets ANY origin in the
# operator's browser cache read those bytes; an attacker-controlled
# page could pull the current playing frame off the captive-portal AP.
# Replace with allowlist-reflective echoing: only set ACAO when the
# Origin matches the operator's flock-peer allowlist (or the built-in
# local origins -- localhost/AP IP/loopback). Failing closed means the
# browser blocks the cross-origin read; flock peers continue to work
# because they're explicitly allow-listed via the operator's flock
# config.

# Built-in same-network origins that always pass without flock-peer
# enumeration. captive-portal AP IP is the operator's phone hitting
# the device directly during setup; loopback covers dev + the device
# talking to itself.
_BUILTIN_ALLOWLIST_HOSTS: frozenset[str] = frozenset({
    "localhost",
    "127.0.0.1",
    "192.168.4.1",  # captive-portal AP gateway (SYSTEM_SPEC §4.1)
})


def _parse_origin_host(origin: str) -> str | None:
    """Extract the bare host from an `Origin` header value.

    `Origin` per RFC 6454 is `<scheme>://<host>[:<port>]` (no path, no
    query, no fragment). Defensive against malformed values: bare host
    without scheme is accepted (some embedded clients omit it); empty
    or unparseable returns None.
    """
    if not origin:
        return None
    # Strip scheme.
    if "://" in origin:
        origin = origin.split("://", 1)[1]
    # Strip path (defensive; shouldn't appear in Origin).
    if "/" in origin:
        origin = origin.split("/", 1)[0]
    # Strip port.
    if ":" in origin:
        origin = origin.split(":", 1)[0]
    host = origin.strip().lower()
    return host or None


def origin_in_flock_allowlist(
    origin: str, flock_storage: FlockStorage
) -> bool:
    """Decide whether a request's `Origin` should receive a reflective
    `Access-Control-Allow-Origin` header.

    Allowed:
      - localhost / 127.0.0.1 / 192.168.4.1 (built-in same-network)
      - Any peer.address from the operator's flock (port stripped,
        case-insensitive)

    All other origins return False; caller should omit the ACAO header
    entirely (browser blocks cross-origin read). Failing closed.

    Same-origin requests don't reach this helper -- the browser doesn't
    send an `Origin` header for same-origin GETs, so the caller's
    `origin = ""` branch falls through cleanly.
    """
    host = _parse_origin_host(origin)
    if host is None:
        return False
    if host in _BUILTIN_ALLOWLIST_HOSTS:
        return True
    flock = flock_storage.load()
    for peer in flock.peers:
        # peer.address is already normalised to lowercase; may include
        # ":<port>". Strip the port for host-level comparison so a
        # peer registered as `lobby.ts.net:8000` matches an Origin of
        # `https://lobby.ts.net` (default ports).
        peer_host = peer.address.split(":", 1)[0]
        if peer_host == host:
            return True
    return False


def cors_headers_for_origin(
    origin: str, flock_storage: FlockStorage
) -> dict[str, str]:
    """Build the response-header dict for a flock-readable endpoint.

    Returns `{ACAO: <origin>, Vary: "Origin"}` when allow-listed, or
    `{}` when not. Vary tells caches the response depends on Origin so
    a peer's allowed response doesn't get served to an unallowed
    origin from a shared cache.
    """
    if origin_in_flock_allowlist(origin, flock_storage):
        return {
            "Access-Control-Allow-Origin": origin,
            "Vary": "Origin",
        }
    return {}


def _append_to_playlist(playlist_storage: PlaylistStorage, item_id) -> None:
    """Idempotent append: load → append → save."""
    playlist = playlist_storage.load()
    playlist.append(item_id)
    playlist_storage.save(playlist)


def _remove_from_playlist(playlist_storage: PlaylistStorage, item_id) -> None:
    """Idempotent remove: load → remove → save (no-op if absent)."""
    playlist = playlist_storage.load()
    playlist.remove(item_id)
    playlist_storage.save(playlist)


def _decode_image_payload(b64: str) -> bytes:
    """Decode a base64 image payload (PNG or JPG) and verify it's
    structurally an image. Bytes are returned verbatim — the storage
    layer keeps whichever format the operator uploaded, and the
    playback renderer's Pillow open handles either.
    """
    try:
        data = base64.b64decode(b64, validate=True)
    except ValueError as exc:
        # 11.2: don't reflect exception string into response.
        raise HTTPException(
            status_code=400, detail="image_base64 is not valid base64"
        ) from exc
    try:
        # 11.1 / sweep #5 #5: use .load() not .verify() at the upload
        # boundary. verify() only walks structural metadata; load()
        # actually runs the codec, so a malicious image that uses a
        # known-bad-decoder path (e.g. Pillow CVE-2026-25990 et al.)
        # surfaces here at upload time, not later at playback when
        # the device is composing slides.
        with Image.open(io.BytesIO(data)) as img:
            img.load()
    except (UnidentifiedImageError, Exception) as exc:
        raise HTTPException(
            status_code=400,
            detail="image_base64 decoded but isn't a valid image",
        ) from exc
    return data


def _validation_error_422(exc: ValidationError) -> HTTPException:
    """Translate a Pydantic ValidationError into a JSON-safe 422.

    `exc.errors()` returns Python objects that include UUIDs (in `input`)
    and raw Exception instances (in `ctx`), neither of which the FastAPI
    JSON encoder handles by default — leaving them in flips a 422 into a
    500. `exc.json()` round-trips through pydantic's own serializer so
    every field is guaranteed JSON-safe; we parse back to a list so the
    HTTPException detail is a structured array rather than an opaque
    string. `include_input=False` also drops echoed payload values, which
    we don't want to reflect back in the response body anyway.
    """
    return HTTPException(
        status_code=422,
        detail=json.loads(exc.json(include_input=False)),
    )


def _decode_png_payload(b64: str) -> bytes:
    """Decode a base64 string and confirm it's actually a PNG.

    The browser side restricts file picker types and uses canvas.toBlob, so a
    well-behaved client always sends a valid PNG. But the captive-portal API
    is exposed to anything on the AP's WiFi — defense in depth says we don't
    persist uninterpretable bytes that the playback engine would later have
    to log+skip.

    Raises HTTPException(400) for either bad base64 or bad image bytes.
    """
    try:
        png = base64.b64decode(b64, validate=True)
    except ValueError as exc:  # binascii.Error subclasses ValueError
        # 11.2: don't reflect exception string into response.
        raise HTTPException(
            status_code=400, detail="png_base64 is not valid base64"
        ) from exc

    try:
        # 11.1 / sweep #5 #5: .load() not .verify() -- runs the
        # codec so a Pillow CVE path surfaces at upload time.
        with Image.open(io.BytesIO(png)) as img:
            img.load()
    except (UnidentifiedImageError, Exception) as exc:
        raise HTTPException(
            status_code=400,
            detail="png_base64 decoded but isn't a valid image",
        ) from exc

    return png


class TextLayerUpload(BaseModel):
    """Per-layer wire format inside a TextSlideUpload. Mirrors TextLayer's
    fields without the validators (those fire when the route reconstructs
    the canonical model and the route catches their ValidationError → 422)."""

    text: str
    name: str | None = None
    font_family: str | None = None
    font_size_px: int | None = None
    font_size_pct: float | None = None
    weight: int | None = None
    text_color: str = "#FFFFFF"
    text_align: str | None = None
    outline: bool | None = None
    opacity: float | None = None
    anchor: str | None = None
    visible: bool | None = None
    locked: bool | None = None
    motion: str | None = None
    motion_intensity: int | None = None
    motion_phase: float | None = None
    motion_speed: float | None = None
    blend: str | None = None
    auto_mode: str | None = None
    auto_format: str | None = None
    box: TextBox | None = None


class TextSlideUpload(BaseModel):
    """Wire format for POST /api/content/text-slides.

    Same shape as TextSlide minus id/created_at (server assigns those), plus
    the rendered PNG as base64. Base64 adds ~33% transfer bloat but text-slide
    PNGs are KB-sized and the simpler all-JSON contract beats multipart for
    client code.

    Schema v3 (qarl 2026-05-01): text fields live in `text_layers`, a list
    of TextLayerUpload (one entry per layer; index 0 draws first, later
    entries composite over earlier ones).

    Field constraints (length caps, hex color pattern, auto_format/auto_mode
    cross-validation, box bounds) live on TextSlide / TextLayer so there's
    a single source of truth — the route catches the resulting
    ValidationError and returns 422.
    """

    name: str
    duration_ms: int = 5000
    background_color: str = "#000000"
    background_image_slide_id: UUID | None = None
    background_video_slide_id: UUID | None = None
    # Procedural pattern background (one of 11 — gradient, dots,
    # halftone, stripes, scanlines, checker, rings, rays, confetti,
    # bricks, solid). Mutex with the image / video bg refs; the
    # cross-field validation lives on TextSlide so this just mirrors
    # the wire shape. Replaces `background_gradient` (qarl 2026-05-03
    # designer handoff). Adding a field on TextSlide without also
    # adding it here is the silent-drop bug shape that has bitten us
    # FOUR times now (motion_intensity, motion_phase, gradient,
    # pattern) — the generic test in test_textslide_field_round_trip.
    # py is the regression guard so it doesn't bite a fifth.
    background_pattern: BackgroundPattern | None = None
    transition: str = "cut"
    transition_ms: int = 500
    text_layers: list[TextLayerUpload]
    png_base64: str = Field(description="Base64-encoded PNG of the rendered slide.")


@router.post("/text-slides", response_model=TextSlide)
async def upload_text_slide(
    payload: TextSlideUpload,
    storage: StorageDep,
    playlist_storage: PlaylistDep,
    flock_sync: FlockSyncDep,
    background: BackgroundTasks,
) -> TextSlide:
    png = _decode_png_payload(payload.png_base64)

    # All field constraints live on TextSlide; the route surfaces violations
    # as 422 instead of letting them become 500s. exclude_none so a
    # not-supplied `box` falls back to TextSlide's default_factory rather
    # than failing on `box=None` (TextBox isn't optional on the model).
    try:
        slide = TextSlide(
            **payload.model_dump(exclude={"png_base64"}, exclude_none=True)
        )
    except ValidationError as exc:
        raise _validation_error_422(exc) from exc

    storage.save_text_slide(slide, png)
    _append_to_playlist(playlist_storage, slide.id)
    background.add_task(flock_sync.notify_peers, slide.id, "updated")
    return slide


@router.put("/text-slides/{item_id}", response_model=TextSlide)
async def update_text_slide(
    item_id: UUID,
    payload: TextSlideUpload,
    storage: StorageDep,
    flock_sync: FlockSyncDep,
    background: BackgroundTasks,
) -> TextSlide:
    """Replace an existing text slide. Used by the editor's edit-existing
    flow — operator clicks a pallet tile, tweaks, saves. The slide keeps
    its UUID, so playlist + schedule references remain valid.

    Non-TextSlide variants (ImageSlide / VideoSlide) aren't editable via
    this route — clients should check item type and either POST a fresh
    slide or not offer the edit affordance.
    """
    # Refuse if the target exists but isn't a TextSlide — the only shape
    # this endpoint can honor.
    try:
        existing = storage.load(item_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no text slide {item_id}") from exc
    if existing.type != "text_slide":
        raise HTTPException(
            status_code=409,
            detail=f"{item_id} is a {existing.type}, not a text_slide",
        )

    png = _decode_png_payload(payload.png_base64)
    try:
        # Preserve the id + created_at; let everything else come from the
        # payload. exclude_none so a not-supplied `box` falls back to
        # TextSlide's default_factory rather than failing on `box=None`.
        slide = TextSlide(
            id=item_id,
            created_at=existing.created_at,
            **payload.model_dump(exclude={"png_base64"}, exclude_none=True),
        )
    except ValidationError as exc:
        raise _validation_error_422(exc) from exc

    storage.save_text_slide(slide, png)
    background.add_task(flock_sync.notify_peers, slide.id, "updated")
    return slide


def _decode_mp4_payload(b64: str) -> bytes:
    """Decode + sanity-check an MP4 upload.

    The real validation of H.264 profile / level / dimensions happens
    client-side in ffmpeg.wasm (future); here we just confirm the file
    starts with a well-formed MP4 `ftyp` box so we're not persisting
    text / images / random bytes under asset.mp4.

    Bug 8 / Fix C (2026-05-17): also validates the file carries an
    H.264 video trak. Multi-trak (2026-05-20): the rust sidecar now
    SELECTS the video trak out of a multi-trak container, so a
    video+audio MP4 is fully supported -- the audio trak is simply
    ignored. The upload gate therefore rejects only a file with NO
    video trak (or a malformed container): such a file reaches
    cache.load, fails to demux, gets skip-marked via Fix A, and the
    operator gets a confusing "Unsupported" log instead of a clear
    upload-time rejection. Rejecting at upload is the right surface.
    """
    try:
        mp4 = base64.b64decode(b64, validate=True)
    except ValueError as exc:
        # 11.2: don't reflect exception string into response.
        raise HTTPException(
            status_code=400, detail="mp4_base64 is not valid base64"
        ) from exc

    # MP4 files start with a box: 4 bytes big-endian size, then 4 bytes type.
    # The first box is almost always `ftyp`. We tolerate any ftyp brand.
    if len(mp4) < 12 or mp4[4:8] != b"ftyp":
        raise HTTPException(
            status_code=400,
            detail="mp4_base64 doesn't look like an MP4 (missing ftyp box)",
        )

    video_trak_count = _count_video_traks_in_mp4(mp4)
    if video_trak_count == 0:
        # The rust sidecar (renderer/src/mp4_demux.rs ::
        # select_video_mdia) bails with "no video trak ..." if
        # cache.load sees a container with no 'vide'-handler trak.
        # Pre-Bug-8 a non-playable file slipped past upload and
        # surfaced as a paint-time OpError that hot-spun the
        # playback loop. Reject at upload with an actionable
        # message instead.
        raise HTTPException(
            status_code=400,
            detail=(
                "MP4 has no H.264 video trak. openMarquee plays the "
                "video trak of an MP4 (an audio trak is fine and is "
                "ignored). Re-export a real H.264 video, e.g. "
                "`ffmpeg -i input -c:v libx264 output.mp4`"
            ),
        )
    if video_trak_count < 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "MP4 box structure is malformed (truncated or invalid "
                "size fields). Re-export with: `ffmpeg -i input.mp4 "
                "-c:v libx264 output.mp4`"
            ),
        )

    return mp4


def _count_video_traks_in_mp4(mp4: bytes) -> int:
    """Walk top-level boxes to find `moov`, then count `trak`
    children whose `mdia`/`hdlr` advertises a 'vide' handler.

    Mirror of `renderer/src/mp4_demux.rs::select_video_mdia` so an
    upload the rust sidecar would reject at cache.load gets rejected
    here at upload time with an actionable error message. The
    renderer demuxes the first video trak and ignores audio /
    timecode traks, so a video+audio MP4 is fully supported; only a
    file with *no* video trak (or a malformed container) fails.

    Returns:
      - Non-negative integer: count of video traks inside the moov
        box. Audio / timecode traks are not counted.
      - -1: malformed box structure (truncated header, size < 8,
        size overflows parent, or no moov box).

    Box format: 4 bytes big-endian size, 4 bytes type tag, then
    `size-8` bytes of payload. No 64-bit extended-size support
    (mp4_demux.rs doesn't support it either — what rust would
    reject at runtime, upload rejects here).
    """

    class _Malformed(Exception):
        """Raised on any invalid box size field."""

    def _child_boxes(payload: bytes) -> list[tuple[bytes, int, int]]:
        """`(tag, body_start, body_end)` for each direct child box;
        offsets index into `payload`. Records offsets rather than
        slicing so a large `mdat` is never copied."""
        boxes: list[tuple[bytes, int, int]] = []
        pos = 0
        while pos + 8 <= len(payload):
            size = int.from_bytes(payload[pos:pos + 4], "big")
            kind = payload[pos + 4:pos + 8]
            if size < 8 or pos + size > len(payload):
                raise _Malformed
            boxes.append((kind, pos + 8, pos + size))
            pos += size
        return boxes

    def _first_child(payload: bytes, tag: bytes) -> bytes | None:
        for kind, start, end in _child_boxes(payload):
            if kind == tag:
                return payload[start:end]
        return None

    try:
        moov = _first_child(mp4, b"moov")
        if moov is None:
            return -1
        n = 0
        for kind, start, end in _child_boxes(moov):
            if kind != b"trak":
                continue
            # A trak with no mdia/hdlr is skipped here, not counted.
            # The rust select_video_mdia is stricter (it bails on a
            # trak missing those children) -- this asymmetry only
            # affects hand-crafted malformed input: the renderer
            # stays the authority, so nothing unplayable slips to
            # playback; the upload error is just less precise.
            mdia = _first_child(moov[start:end], b"mdia")
            if mdia is None:
                continue
            hdlr = _first_child(mdia, b"hdlr")
            # hdlr body: 1-byte version + 3-byte flags + 4-byte
            # pre_defined + 4-byte handler_type. handler_type is
            # at offset 8..12.
            if hdlr is not None and len(hdlr) >= 12 and hdlr[8:12] == b"vide":
                n += 1
        return n
    except _Malformed:
        return -1


class VideoUpload(BaseModel):
    """Wire format for POST /api/content/videos.

    Always H.264 in MP4. Browser-side ffmpeg.wasm caps the transcode at
    min(source, 1920×1080) to stay inside the Pi Zero 2 W's hardware
    H.264 decoder envelope; playback further scales down to the current
    panel dims via ffmpeg's filter graph at decode time.
    """

    name: str
    duration_ms: int = 5000
    transition: str = "cut"
    transition_ms: int = 500
    png_base64: str = Field(description="Thumbnail PNG (first frame).")
    mp4_base64: str = Field(description="H.264 MP4 bytes, ≤ 1080p.")


class VideoUpdate(BaseModel):
    """Wire format for PUT /api/content/videos/{id}.

    Asset bodies (png_base64 / mp4_base64) are optional: omit to keep
    existing bytes. Metadata always updates.
    """

    name: str
    duration_ms: int = 5000
    png_base64: str | None = None
    mp4_base64: str | None = None


@router.post("/videos", response_model=VideoSlide)
async def upload_video(
    payload: VideoUpload,
    storage: StorageDep,
    playlist_storage: PlaylistDep,
    flock_sync: FlockSyncDep,
    background: BackgroundTasks,
) -> VideoSlide:
    thumbnail = _decode_png_payload(payload.png_base64)
    mp4 = _decode_mp4_payload(payload.mp4_base64)
    try:
        video = VideoSlide(**payload.model_dump(exclude={"png_base64", "mp4_base64"}))
    except ValidationError as exc:
        raise _validation_error_422(exc) from exc
    storage.save_video(video, thumbnail, mp4)
    _append_to_playlist(playlist_storage, video.id)
    background.add_task(flock_sync.notify_peers, video.id, "updated")
    return video


class ImageUpload(BaseModel):
    """Wire format for POST /api/content/images.

    The browser uploads the operator's SOURCE bytes (PNG/JPG/etc) verbatim
    — no Canvas-scaling round-trip. We keep the file at its full original
    resolution; the playback engine cover-fits to panel dims on slide entry,
    so a panel resize never degrades the stored asset.
    """

    name: str
    duration_ms: int = 5000
    transition: str = "cut"
    transition_ms: int = 500
    image_base64: str = Field(description="Base64-encoded source image (PNG or JPG, verbatim).")


@router.post("/images", response_model=ImageSlide)
async def upload_image(
    payload: ImageUpload,
    storage: StorageDep,
    playlist_storage: PlaylistDep,
    flock_sync: FlockSyncDep,
    background: BackgroundTasks,
) -> ImageSlide:
    image_bytes = _decode_image_payload(payload.image_base64)

    try:
        image = ImageSlide(**payload.model_dump(exclude={"image_base64"}))
    except ValidationError as exc:
        raise _validation_error_422(exc) from exc

    storage.save_image(image, image_bytes)
    _append_to_playlist(playlist_storage, image.id)
    background.add_task(flock_sync.notify_peers, image.id, "updated")
    return image


class ImageUpdate(BaseModel):
    """Wire format for PUT /api/content/images/{id}.

    `image_base64` is optional: omit to keep existing bytes and update
    only metadata (name / duration). Pass new source bytes to replace
    the stored asset in place; the UUID stays the same so playlist +
    schedule references are preserved.
    """

    name: str
    duration_ms: int = 5000
    image_base64: str | None = None


@router.put("/images/{item_id}", response_model=ImageSlide)
async def update_image(
    item_id: UUID,
    payload: ImageUpdate,
    storage: StorageDep,
    flock_sync: FlockSyncDep,
    background: BackgroundTasks,
) -> ImageSlide:
    try:
        existing = storage.load(item_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no image {item_id}") from exc
    if existing.type != "image":
        raise HTTPException(
            status_code=409,
            detail=f"{item_id} is a {existing.type}, not an image",
        )

    try:
        updated = ImageSlide(
            id=item_id,
            created_at=existing.created_at,
            name=payload.name,
            duration_ms=payload.duration_ms,
        )
    except ValidationError as exc:
        raise _validation_error_422(exc) from exc

    # No new bytes → keep the existing asset on disk untouched.
    image_bytes = (
        _decode_image_payload(payload.image_base64)
        if payload.image_base64
        else storage.read_asset(item_id)
    )
    storage.save_image(updated, image_bytes)
    background.add_task(flock_sync.notify_peers, updated.id, "updated")
    return updated


class VlcStreamUpload(BaseModel):
    """Wire format for POST /api/content/vlc-streams.

    A VLC-stream slide has no operator-uploaded asset — the video is a
    live RTSP feed and the thumbnail card is synthesised at save time
    (storage.save_vlc_stream). The payload is therefore pure metadata.
    `on_unreachable` / `transition` are plain strings here; the
    VlcStreamSlide model does the Literal validation (a bad value
    surfaces as a 422), mirroring how VideoUpload.transition works.
    """

    name: str
    rtsp_url: str
    duration_ms: int = 10_000
    on_unreachable: str = "hold_last_frame"
    transition: str = "cut"
    transition_ms: int = 500


@router.post("/vlc-streams", response_model=VlcStreamSlide)
async def upload_vlc_stream(
    payload: VlcStreamUpload,
    storage: StorageDep,
    playlist_storage: PlaylistDep,
    flock_sync: FlockSyncDep,
    background: BackgroundTasks,
) -> VlcStreamSlide:
    try:
        slide = VlcStreamSlide(**payload.model_dump())
    except ValidationError as exc:
        raise _validation_error_422(exc) from exc
    storage.save_vlc_stream(slide)
    _append_to_playlist(playlist_storage, slide.id)
    background.add_task(flock_sync.notify_peers, slide.id, "updated")
    return slide


class VlcStreamUpdate(BaseModel):
    """Wire format for PUT /api/content/vlc-streams/{id}. All metadata;
    the UUID is preserved so playlist + schedule references hold."""

    name: str
    rtsp_url: str
    duration_ms: int = 10_000
    on_unreachable: str = "hold_last_frame"
    transition: str = "cut"
    transition_ms: int = 500


@router.put("/vlc-streams/{item_id}", response_model=VlcStreamSlide)
async def update_vlc_stream(
    item_id: UUID,
    payload: VlcStreamUpdate,
    storage: StorageDep,
    flock_sync: FlockSyncDep,
    background: BackgroundTasks,
) -> VlcStreamSlide:
    try:
        existing = storage.load(item_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"no vlc stream {item_id}"
        ) from exc
    if existing.type != "vlc_stream":
        raise HTTPException(
            status_code=409,
            detail=f"{item_id} is a {existing.type}, not a vlc_stream",
        )
    try:
        updated = VlcStreamSlide(
            id=item_id,
            created_at=existing.created_at,
            **payload.model_dump(),
        )
    except ValidationError as exc:
        raise _validation_error_422(exc) from exc
    storage.save_vlc_stream(updated)
    background.add_task(flock_sync.notify_peers, updated.id, "updated")
    return updated


@router.get("", response_model=list[ContentItem])
async def list_content(storage: StorageDep, playlist_storage: PlaylistDep) -> list[ContentItem]:
    """All saved content, ordered by the playlist (orphans appended at end).

    Same ordering used by the playback engine — UI list and what plays on the
    sign stay in sync.
    """
    # UI pallets + bg-picker want the full library — orphans included —
    # so bundled seed content (backgrounds, demo videos) is visible for
    # the operator to drag into a playlist. Playback stays strict (see
    # scheduled_fetch_items) so orphans don't leak onto the sign.
    return list_in_playlist_order(storage, playlist_storage, include_orphans=True)


@router.get("/{item_id}", response_model=ContentItem)
async def get_content_item(item_id: UUID, storage: StorageDep) -> ContentItem:
    try:
        return storage.load(item_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no content item {item_id}") from exc


@router.get(
    "/{item_id}/asset",
    response_class=FileResponse,
    responses={
        200: {"content": {"image/png": {}}},
        404: {"description": "No asset for that id."},
    },
)
async def get_asset(item_id: UUID, storage: StorageDep) -> FileResponse:
    path = storage.asset_path(item_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no asset for {item_id}")
    return FileResponse(path, media_type="image/png")


@router.put("/videos/{item_id}", response_model=VideoSlide)
async def update_video(
    item_id: UUID,
    payload: VideoUpdate,
    storage: StorageDep,
    flock_sync: FlockSyncDep,
    background: BackgroundTasks,
) -> VideoSlide:
    try:
        existing = storage.load(item_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no video {item_id}") from exc
    if existing.type != "video":
        raise HTTPException(
            status_code=409,
            detail=f"{item_id} is a {existing.type}, not a video",
        )

    try:
        updated = VideoSlide(
            id=item_id,
            created_at=existing.created_at,
            name=payload.name,
            duration_ms=payload.duration_ms,
        )
    except ValidationError as exc:
        raise _validation_error_422(exc) from exc

    # Thumbnail: reuse existing PNG on metadata-only saves, decode + save
    # a new one when provided.
    thumbnail = (
        _decode_png_payload(payload.png_base64)
        if payload.png_base64
        else storage.read_asset(item_id)
    )
    mp4 = (
        _decode_mp4_payload(payload.mp4_base64)
        if payload.mp4_base64
        else storage.read_video(item_id)
    )
    storage.save_video(updated, thumbnail, mp4)
    background.add_task(flock_sync.notify_peers, updated.id, "updated")
    return updated


@router.get(
    "/{item_id}/video",
    response_class=FileResponse,
    responses={
        200: {"content": {"video/mp4": {}}},
        404: {"description": "No video for that id."},
    },
)
async def get_video(item_id: UUID, storage: StorageDep) -> FileResponse:
    """Serve an MP4 payload. Distinct endpoint from /asset (the thumbnail)."""
    path = storage.video_path(item_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no video for {item_id}")
    return FileResponse(path, media_type="video/mp4")


@router.delete("/{item_id}", status_code=204)
async def delete_content_item(
    item_id: UUID,
    storage: StorageDep,
    playlist_storage: PlaylistDep,
    tombstones: TombstoneDep,
    flock_sync: FlockSyncDep,
    background: BackgroundTasks,
) -> None:
    # 404 first — no tombstone for an id we never had.
    if not storage.exists(item_id):
        raise HTTPException(status_code=404, detail=f"no content item {item_id}")
    # Tombstone BEFORE delete. If tombstone.add fails we'd rather bail and
    # leave the content in place than end up with a silent delete that
    # syncing peers can't learn about (resurrect-on-next-pull). The reverse
    # order is not self-healing.
    tombstones.add(item_id)
    storage.delete(item_id)
    _remove_from_playlist(playlist_storage, item_id)
    background.add_task(flock_sync.notify_peers, item_id, "deleted")


class DurationPatch(BaseModel):
    """Wire format for PATCH /api/content/{id}/duration."""

    duration_ms: int = Field(ge=100, le=24 * 60 * 60 * 1000)


@router.patch("/{item_id}/duration", response_model=ContentItem)
async def patch_duration(
    item_id: UUID,
    payload: DurationPatch,
    storage: StorageDep,
    flock_sync: FlockSyncDep,
    background: BackgroundTasks,
) -> ContentItem:
    """Update just the duration of a content item — used by the
    Playlists-panel duration chip so the operator can change a slide's
    seconds without re-PUTting the full payload (which would require
    re-encoding the PNG / MP4 / RGB asset for nothing).
    """
    try:
        item = storage.load(item_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no content item {item_id}") from exc
    try:
        updated = item.model_copy(update={"duration_ms": payload.duration_ms})
    except ValidationError as exc:
        raise _validation_error_422(exc) from exc
    # save() rewrites the envelope; the on-disk PNG stays untouched.
    existing_png = storage.read_asset(item_id)
    storage.save(updated, existing_png)
    background.add_task(flock_sync.notify_peers, updated.id, "updated")
    return updated
