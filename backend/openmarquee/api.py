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

    Client provides:
      - `png_base64`: thumbnail frame (the UI extracts the first frame
        from the chosen file via a <video> element + canvas).
      - `mp4_base64`: the video bytes themselves. Client-side encoding
        via ffmpeg.wasm is TODO; today the browser uploads whatever MP4
        the user picked.

    The base64+JSON framing costs ~33% over multipart but keeps the
    client code a single JSON POST. For the Pi Zero 2 W's 512 MB RAM
    this caps practical video size to somewhere around ~100 MB encoded
    — acceptable for short clips at demo resolutions.
    """

    name: str
    duration_ms: int = 5000
    pipeline: str = "h264_mp4"
    transition: str = "cut"
    transition_ms: int = 500
    png_base64: str = Field(description="Thumbnail PNG (first frame).")
    mp4_base64: str = Field(description="MP4 H.264 video bytes.")


@router.post("/videos", response_model=VideoSlide)
async def upload_video(
    payload: VideoUpload,
    storage: StorageDep,
    playlist_storage: PlaylistDep,
) -> VideoSlide:
    # Reject raw_frames uploads until the storage-for-frame-sequences path
    # lands. The VideoSlide model accepts the value so existing data can
    # round-trip, but the upload endpoint can only persist `asset.mp4`
    # today — accepting a raw_frames payload would save an MP4 mis-
    # labeled as raw_frames and break the panel renderers when they ship.
    # Operators producing raw RGB frames via the /spike.html page today
    # should byte-compare against ffmpeg CLI, not upload via this route.
    if payload.pipeline == "raw_frames":
        raise HTTPException(
            status_code=422,
            detail=(
                "raw_frames pipeline is not yet accepted by the upload "
                "endpoint — use the /spike.html page to produce raw RGB "
                "frames, or upload H.264 MP4 via pipeline='h264_mp4'."
            ),
        )

    thumbnail = _decode_png_payload(payload.png_base64)
    mp4 = _decode_mp4_payload(payload.mp4_base64)

    try:
        video = VideoSlide(**payload.model_dump(exclude={"png_base64", "mp4_base64"}))
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
    item_id: UUID, storage: StorageDep, playlist_storage: PlaylistDep
) -> None:
    try:
        storage.delete(item_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no content item {item_id}") from exc
    _remove_from_playlist(playlist_storage, item_id)
