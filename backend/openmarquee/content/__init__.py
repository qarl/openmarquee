"""Content model — typed descriptions of things that can be played on the sign.

Every variant carries a `type` literal, and `ContentItem` is a discriminated
union over them. Pydantic picks the right subclass from the `type` field on
the wire.

What's deliberately *not* in the model: where any rendered asset lives on disk.
That's the storage layer's job (`openmarquee.content.storage`). Models are pure
metadata; storage maps an item's `id` to bytes.

Variants today: `TextSlide`, `ImageSlide`, `VideoSlide`.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

# Hex color regex: #RRGGBB. Six lowercase or uppercase hex digits.
_HEX_COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"


def _utcnow() -> datetime:
    """Timezone-aware UTC now. (datetime.utcnow is deprecated in 3.12+.)"""
    return datetime.now(UTC)


class TextSlide(BaseModel):
    """A user-typed text slide.

    The browser renders the text to a PNG at the sign's native resolution
    using Canvas, then uploads the PNG. The editor metadata (text, font,
    colors, etc.) is kept on the device so the slide can be re-opened and
    edited without losing fidelity.

    `auto_mode` (optional) marks a slide as *dynamic* — its visible text
    is regenerated at playback time from a device-side data source
    (current time, today's date, day-of-week, …). When set, the stored
    `text` field acts as a fallback for previews and list thumbnails; the
    authoritative content comes from the live render. `None` = static
    (the stored PNG is what plays). Lands as a metadata-only field here;
    the playback-time render-over path is follow-up work.
    """

    type: Literal["text_slide"] = "text_slide"
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(max_length=200)
    duration_ms: int = Field(default=5000, ge=100)

    # Editor metadata — what the user typed and how they styled it.
    text: str = Field(max_length=10_000)
    font_family: str | None = None
    font_size_px: int | None = Field(default=None, ge=4, le=2048)
    text_color: str = Field(default="#FFFFFF", pattern=_HEX_COLOR_PATTERN)
    background_color: str = Field(default="#000000", pattern=_HEX_COLOR_PATTERN)
    # Optional: render a bundled / saved ImageSlide's PNG under the text.
    # The UI flattens both layers to a single PNG at save time, so the
    # device doesn't re-composite at playback — we keep the reference for
    # re-editing (operator clicks a saved TextSlide; the editor hydrates
    # the background picker with the right selection).
    background_image_slide_id: UUID | None = None
    auto_mode: Literal["time", "date", "day"] | None = None

    # Transition INTO the next slide ("cut" = instant; "fade" = alpha-blend
    # across `transition_ms` after this slide's duration ends).
    transition: Literal["cut", "fade"] = "cut"
    transition_ms: int = Field(default=500, ge=0, le=5000)

    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("text_color", "background_color")
    @classmethod
    def _uppercase_hex(cls, value: str) -> str:
        """Canonicalize hex colors to uppercase so `#ffaa00` and `#FFAA00`
        compare and dedupe as the same value."""
        return value.upper()


class ImageSlide(BaseModel):
    """A user-uploaded image.

    Contract: the browser scales the source JPG/PNG to the sign's native
    resolution via Canvas and uploads the result as PNG — the backend only
    ever sees pre-scaled pixel data. The model itself is minimal because
    all the pixels live in the asset file; the envelope just carries
    housekeeping metadata.
    """

    type: Literal["image"] = "image"
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(max_length=200)
    duration_ms: int = Field(default=5000, ge=100)

    # Same transition contract as TextSlide.
    transition: Literal["cut", "fade"] = "cut"
    transition_ms: int = Field(default=500, ge=0, le=5000)

    created_at: datetime = Field(default_factory=_utcnow)


class VideoSlide(BaseModel):
    """A user-uploaded video.

    Today's contract: the browser uploads an MP4 (H.264) directly plus a
    PNG thumbnail (typically the first frame). The backend stores both:

        <id>/asset.png   — thumbnail (used by the saved-slides list)
        <id>/asset.mp4   — MP4 payload (HDMI renderer streams from here)

    The ffmpeg.wasm client-side pipeline (decode → scale → re-encode MP4
    for HDMI, OR extract raw RGB frames for HUB75/WS2812B/composite) is
    the follow-up. For now the browser passes through whatever MP4 the
    user picked; if the source is too big for the Pi Zero 2 W's hardware
    H.264 decoder, playback on HDMI will stutter and the operator will
    learn to pre-encode. The spec accepts the rough edge — ffmpeg.wasm
    transcoding lands when the HDMI renderer does.

    `duration_ms` on a video is *informational*: the playback engine reads
    the actual runtime from the file, and this field is just what the UI
    renders in the saved-slides list. Keeping it present so the schema
    looks like TextSlide/ImageSlide and a single ContentItem union works.
    """

    type: Literal["video"] = "video"
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(max_length=200)
    duration_ms: int = Field(default=5000, ge=100)

    # "h264_mp4" stores an `asset.mp4` (H.264 in an MP4 container) — the
    # HDMI path; relies on the Pi's hardware decoder. "raw_frames" stores
    # an `asset.rgb` (concatenated RGB888 frames, no header) — the panel
    # path for HUB75 / WS2812B / composite where the renderer wants pre-
    # decoded pixel data. Picked at upload time from the device's current
    # output_mode.
    pipeline: Literal["h264_mp4", "raw_frames"] = "h264_mp4"

    # Only populated when pipeline == "raw_frames". The .rgb stream has
    # no header, so the frame dims + fps must travel with the metadata
    # for the renderer to know how to chunk + pace the playback.
    frames_fps: int | None = Field(default=None, ge=1, le=120)
    frames_width: int | None = Field(default=None, ge=1, le=4096)
    frames_height: int | None = Field(default=None, ge=1, le=4096)

    # Same transition contract as TextSlide/ImageSlide — applied on the way
    # out, so a cut/fade into the next slide still works across variants.
    transition: Literal["cut", "fade"] = "cut"
    transition_ms: int = Field(default=500, ge=0, le=5000)

    created_at: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _frames_metadata_matches_pipeline(self) -> "VideoSlide":
        """raw_frames requires all three of fps/width/height; h264_mp4
        rejects any of them. Keeps on-disk metadata unambiguous about
        which renderer path is in play."""
        is_raw = self.pipeline == "raw_frames"
        has_any = any(
            v is not None for v in (self.frames_fps, self.frames_width, self.frames_height)
        )
        has_all = all(
            v is not None for v in (self.frames_fps, self.frames_width, self.frames_height)
        )
        if is_raw and not has_all:
            raise ValueError(
                "raw_frames pipeline requires frames_fps, frames_width, "
                "and frames_height"
            )
        if not is_raw and has_any:
            raise ValueError(
                "frames_* metadata is only valid when pipeline='raw_frames'"
            )
        return self


# Discriminated union of content variants. Pydantic uses the `type` literal to
# route to the right subclass on deserialize.
ContentItem = Annotated[
    TextSlide | ImageSlide | VideoSlide, Field(discriminator="type")
]
