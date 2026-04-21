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

from openmarquee.content import ContentItem, ImageSlide, TextSlide
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


@router.delete("/{item_id}", status_code=204)
async def delete_content_item(
    item_id: UUID, storage: StorageDep, playlist_storage: PlaylistDep
) -> None:
    try:
        storage.delete(item_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no content item {item_id}") from exc
    _remove_from_playlist(playlist_storage, item_id)
