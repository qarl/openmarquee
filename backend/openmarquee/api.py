"""REST API for content items.

Today exposes just the text-slide path (the F&F demo's centerpiece).
Endpoints for image and video upload land alongside their respective
content variants, post-demo.
"""

import base64
import io
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field, ValidationError

from openmarquee.content import ContentItem, ImageSlide, TextSlide, VideoSlide
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import get_content_storage, get_playlist_storage
from openmarquee.playlist import PlaylistStorage, list_in_playlist_order

router = APIRouter(prefix="/api/content", tags=["content"])

StorageDep = Annotated[ContentStorage, Depends(get_content_storage)]
PlaylistDep = Annotated[PlaylistStorage, Depends(get_playlist_storage)]


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
        raise HTTPException(
            status_code=400, detail=f"png_base64 is not valid base64: {exc}"
        ) from exc

    try:
        with Image.open(io.BytesIO(png)) as img:
            img.verify()  # confirms PNG/JPEG/etc structure without full decode
    except (UnidentifiedImageError, Exception) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"png_base64 decoded but isn't a valid image: {exc}",
        ) from exc

    return png


class TextSlideUpload(BaseModel):
    """Wire format for POST /api/content/text-slides.

    Same shape as TextSlide minus id/created_at (server assigns those), plus
    the rendered PNG as base64. Base64 adds ~33% transfer bloat but text-slide
    PNGs are KB-sized and the simpler all-JSON contract beats multipart for
    client code.

    Field constraints (length caps, hex color pattern, etc.) live on TextSlide
    so there's a single source of truth — the route catches the resulting
    ValidationError and returns 422.
    """

    name: str
    duration_ms: int = 5000
    text: str
    font_family: str | None = None
    font_size_px: int | None = None
    text_color: str = "#FFFFFF"
    background_color: str = "#000000"
    background_image_slide_id: UUID | None = None
    auto_mode: str | None = None
    auto_format: str | None = None
    transition: str = "cut"
    transition_ms: int = 500
    png_base64: str = Field(description="Base64-encoded PNG of the rendered slide.")


@router.post("/text-slides", response_model=TextSlide)
async def upload_text_slide(
    payload: TextSlideUpload,
    storage: StorageDep,
    playlist_storage: PlaylistDep,
) -> TextSlide:
    png = _decode_png_payload(payload.png_base64)

    # All field constraints live on TextSlide; the route surfaces violations
    # as 422 instead of letting them become 500s.
    try:
        slide = TextSlide(**payload.model_dump(exclude={"png_base64"}))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    storage.save_text_slide(slide, png)
    _append_to_playlist(playlist_storage, slide.id)
    return slide


@router.put("/text-slides/{item_id}", response_model=TextSlide)
async def update_text_slide(
    item_id: UUID,
    payload: TextSlideUpload,
    storage: StorageDep,
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
        # Preserve the id + created_at; let everything else come from the payload.
        slide = TextSlide(
            id=item_id,
            created_at=existing.created_at,
            **payload.model_dump(exclude={"png_base64"}),
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    storage.save_text_slide(slide, png)
    return slide


def _decode_mp4_payload(b64: str) -> bytes:
    """Decode + sanity-check an MP4 upload.

    The real validation of H.264 profile / level / dimensions happens
    client-side in ffmpeg.wasm (future); here we just confirm the file
    starts with a well-formed MP4 `ftyp` box so we're not persisting
    text / images / random bytes under asset.mp4.
    """
    try:
        mp4 = base64.b64decode(b64, validate=True)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"mp4_base64 is not valid base64: {exc}"
        ) from exc

    # MP4 files start with a box: 4 bytes big-endian size, then 4 bytes type.
    # The first box is almost always `ftyp`. We tolerate any ftyp brand.
    if len(mp4) < 12 or mp4[4:8] != b"ftyp":
        raise HTTPException(
            status_code=400,
            detail="mp4_base64 doesn't look like an MP4 (missing ftyp box)",
        )
    return mp4


class VideoUpload(BaseModel):
    """Wire format for POST /api/content/videos.

    Two pipeline variants share this shape:

    - `pipeline="h264_mp4"` (HDMI path): set `mp4_base64`. The Pi's
      hardware H.264 decoder plays it directly.
    - `pipeline="raw_frames"` (HUB75 / WS2812B / composite): set
      `raw_frames_base64` plus `frames_fps` / `frames_width` /
      `frames_height`. The panel renderer slices the stream into
      fixed-size RGB888 frames and paces playback at frames_fps.

    Exactly one of mp4_base64 / raw_frames_base64 must be set, matching
    the chosen pipeline. Always also send `png_base64` — the thumbnail
    is used by the saved-slides list for both variants.
    """

    name: str
    duration_ms: int = 5000
    pipeline: str = "h264_mp4"
    transition: str = "cut"
    transition_ms: int = 500
    png_base64: str = Field(description="Thumbnail PNG (first frame).")
    mp4_base64: str | None = None
    raw_frames_base64: str | None = None
    frames_fps: int | None = None
    frames_width: int | None = None
    frames_height: int | None = None


class VideoUpdate(BaseModel):
    """Wire format for PUT /api/content/videos/{id}.

    Asset bodies (png_base64 / mp4_base64 / raw_frames_base64) are
    optional: omit to keep existing bytes. Metadata always updates.
    Pipeline switch (mp4 ↔ raw_frames) is allowed only if new asset
    bytes for the target pipeline are provided; otherwise 422.
    """

    name: str
    duration_ms: int = 5000
    pipeline: str = "h264_mp4"
    png_base64: str | None = None
    mp4_base64: str | None = None
    raw_frames_base64: str | None = None
    frames_fps: int | None = None
    frames_width: int | None = None
    frames_height: int | None = None


def _decode_raw_frames_payload(
    b64: str, fps: int | None, width: int | None, height: int | None
) -> bytes:
    """Decode + size-check a raw_frames upload.

    The stream is headerless concatenated RGB888 (3 bytes per pixel).
    We require len(bytes) % (width*height*3) == 0 so the renderer can
    slice it into whole frames without a trailing partial.
    """
    if fps is None or width is None or height is None:
        raise HTTPException(
            status_code=422,
            detail="raw_frames pipeline requires frames_fps, frames_width, frames_height",
        )
    try:
        data = base64.b64decode(b64, validate=True)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"raw_frames_base64 is not valid base64: {exc}",
        ) from exc
    frame_bytes = width * height * 3
    if len(data) == 0 or len(data) % frame_bytes != 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"raw_frames byte count {len(data)} is not a multiple of "
                f"frame size {frame_bytes} ({width}×{height}×3)"
            ),
        )
    return data


@router.post("/videos", response_model=VideoSlide)
async def upload_video(
    payload: VideoUpload,
    storage: StorageDep,
    playlist_storage: PlaylistDep,
) -> VideoSlide:
    thumbnail = _decode_png_payload(payload.png_base64)

    if payload.pipeline == "raw_frames":
        if payload.mp4_base64 is not None:
            raise HTTPException(
                status_code=422,
                detail="pipeline='raw_frames' rejects mp4_base64 — set raw_frames_base64 instead",
            )
        if payload.raw_frames_base64 is None:
            raise HTTPException(
                status_code=422,
                detail="pipeline='raw_frames' requires raw_frames_base64",
            )
        frames = _decode_raw_frames_payload(
            payload.raw_frames_base64,
            payload.frames_fps,
            payload.frames_width,
            payload.frames_height,
        )
        try:
            video = VideoSlide(
                **payload.model_dump(
                    exclude={"png_base64", "mp4_base64", "raw_frames_base64"}
                )
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        storage.save_video_raw_frames(video, thumbnail, frames)
    else:
        if payload.raw_frames_base64 is not None:
            raise HTTPException(
                status_code=422,
                detail="pipeline='h264_mp4' rejects raw_frames_base64 — set mp4_base64 instead",
            )
        if payload.mp4_base64 is None:
            raise HTTPException(
                status_code=422,
                detail="pipeline='h264_mp4' requires mp4_base64",
            )
        if any(
            v is not None
            for v in (payload.frames_fps, payload.frames_width, payload.frames_height)
        ):
            raise HTTPException(
                status_code=422,
                detail="frames_* metadata is only valid when pipeline='raw_frames'",
            )
        mp4 = _decode_mp4_payload(payload.mp4_base64)
        try:
            video = VideoSlide(
                **payload.model_dump(
                    exclude={"png_base64", "mp4_base64", "raw_frames_base64"}
                )
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        storage.save_video(video, thumbnail, mp4)

    _append_to_playlist(playlist_storage, video.id)
    return video


class ImageUpload(BaseModel):
    """Wire format for POST /api/content/images.

    The browser scales the source JPG/PNG to the sign's native resolution via
    Canvas and encodes the result as PNG. We only ever see pre-scaled bitmap
    data, so the backend doesn't need to know the source format.
    """

    name: str
    duration_ms: int = 5000
    transition: str = "cut"
    transition_ms: int = 500
    png_base64: str = Field(description="Base64-encoded PNG of the scaled image.")


@router.post("/images", response_model=ImageSlide)
async def upload_image(
    payload: ImageUpload,
    storage: StorageDep,
    playlist_storage: PlaylistDep,
) -> ImageSlide:
    png = _decode_png_payload(payload.png_base64)

    try:
        image = ImageSlide(**payload.model_dump(exclude={"png_base64"}))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    storage.save_image(image, png)
    _append_to_playlist(playlist_storage, image.id)
    return image


class ImageUpdate(BaseModel):
    """Wire format for PUT /api/content/images/{id}.

    `png_base64` is optional: omit to keep the existing image bytes and
    update just the metadata (name / duration). Pass a new PNG to replace
    the stored asset in place — the UUID stays the same so playlist +
    schedule references are preserved.
    """

    name: str
    duration_ms: int = 5000
    png_base64: str | None = None


@router.put("/images/{item_id}", response_model=ImageSlide)
async def update_image(
    item_id: UUID,
    payload: ImageUpdate,
    storage: StorageDep,
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
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    # If the client didn't re-upload the image, keep the existing bytes.
    png = (
        _decode_png_payload(payload.png_base64)
        if payload.png_base64
        else storage.read_asset(item_id)
    )
    storage.save_image(updated, png)
    return updated


@router.get("", response_model=list[ContentItem])
async def list_content(storage: StorageDep, playlist_storage: PlaylistDep) -> list[ContentItem]:
    """All saved content, ordered by the playlist (orphans appended at end).

    Same ordering used by the playback engine — UI list and what plays on the
    sign stay in sync.
    """
    return list_in_playlist_order(storage, playlist_storage)


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

    # Pipeline-switch guard: switching h264_mp4 ↔ raw_frames without new
    # asset bytes for the target pipeline would orphan the old asset
    # and leave the new one empty. Require the operator to re-upload.
    pipeline_switching = payload.pipeline != existing.pipeline
    if pipeline_switching:
        if payload.pipeline == "raw_frames" and payload.raw_frames_base64 is None:
            raise HTTPException(
                status_code=422,
                detail="switching to raw_frames requires raw_frames_base64",
            )
        if payload.pipeline == "h264_mp4" and payload.mp4_base64 is None:
            raise HTTPException(
                status_code=422,
                detail="switching to h264_mp4 requires mp4_base64",
            )

    try:
        updated = VideoSlide(
            id=item_id,
            created_at=existing.created_at,
            name=payload.name,
            duration_ms=payload.duration_ms,
            pipeline=payload.pipeline,
            frames_fps=payload.frames_fps
            if payload.pipeline == "raw_frames"
            else None,
            frames_width=payload.frames_width
            if payload.pipeline == "raw_frames"
            else None,
            frames_height=payload.frames_height
            if payload.pipeline == "raw_frames"
            else None,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    # Thumbnail: reuse existing PNG on metadata-only saves, decode + save
    # a new one when provided.
    thumbnail = (
        _decode_png_payload(payload.png_base64)
        if payload.png_base64
        else storage.read_asset(item_id)
    )

    if updated.pipeline == "raw_frames":
        frames = (
            _decode_raw_frames_payload(
                payload.raw_frames_base64,
                payload.frames_fps,
                payload.frames_width,
                payload.frames_height,
            )
            if payload.raw_frames_base64
            else storage.read_video_raw_frames(item_id)
        )
        storage.save_video_raw_frames(updated, thumbnail, frames)
    else:
        mp4 = (
            _decode_mp4_payload(payload.mp4_base64)
            if payload.mp4_base64
            else storage.read_video(item_id)
        )
        storage.save_video(updated, thumbnail, mp4)
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


@router.get(
    "/{item_id}/frames",
    response_class=FileResponse,
    responses={
        200: {"content": {"application/octet-stream": {}}},
        404: {"description": "No raw frames for that id."},
    },
)
async def get_frames(item_id: UUID, storage: StorageDep) -> FileResponse:
    """Serve a raw_frames VideoSlide's concatenated RGB888 bytes.

    Headerless — dimensions + fps come from the VideoSlide metadata via
    /api/content/{id}. Panel renderers (HUB75 / WS2812B / composite)
    stream from this endpoint.
    """
    path = storage.frames_path(item_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no raw frames for {item_id}")
    return FileResponse(path, media_type="application/octet-stream")


@router.delete("/{item_id}", status_code=204)
async def delete_content_item(
    item_id: UUID, storage: StorageDep, playlist_storage: PlaylistDep
) -> None:
    try:
        storage.delete(item_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no content item {item_id}") from exc
    _remove_from_playlist(playlist_storage, item_id)
