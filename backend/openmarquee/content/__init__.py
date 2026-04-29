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
    (current time, today's date, day-of-week). When set, the playback
    loop re-composites the slide each tick using the device's timezone
    via `openmarquee.auto_render`. The stored PNG + `text` field act as
    fallbacks for pallet thumbnails / previews; the authoritative
    playback frames come from the live render.

    `auto_format` pairs with `auto_mode` — a mode-specific format
    choice. Must be None when auto_mode is None, and must be compatible
    with the chosen auto_mode (cross-field validator below). A None
    auto_format falls back to the mode's default format.
    """

    type: Literal["text_slide"] = "text_slide"
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(max_length=200)
    duration_ms: int = Field(default=5000, ge=100)

    # Editor metadata — what the user typed and how they styled it.
    text: str = Field(max_length=10_000)
    font_family: str | None = None
    font_size_px: int | None = Field(default=None, ge=4, le=2048)
    # Optional alternative to font_size_px: a fraction of canvas width.
    # Example: 12.5 = "12.5% of the panel width". When set, takes
    # precedence over font_size_px so a slide saved at 128×96 reads
    # the same proportions on a 1920×1080 panel after a settings flip.
    # Old slides keep font_size_px for backward compat; re-editing
    # them in the unified editor migrates to the relative metric.
    font_size_pct: float | None = Field(default=None, ge=0.5, le=100.0)
    text_color: str = Field(default="#FFFFFF", pattern=_HEX_COLOR_PATTERN)
    background_color: str = Field(default="#000000", pattern=_HEX_COLOR_PATTERN)
    # Optional: render a bundled / saved ImageSlide's PNG under the text.
    # The UI flattens both layers to a single PNG at save time, so the
    # device doesn't re-composite at playback — we keep the reference for
    # re-editing (operator clicks a saved TextSlide; the editor hydrates
    # the background picker with the right selection).
    background_image_slide_id: UUID | None = None
    # Optional: render a saved VideoSlide as the background under the text.
    # Per SYSTEM_SPEC §5.10, the device composites the cached text PNG over
    # each video frame at playback time (live-composite, no pre-bake in
    # v1). The stored thumbnail PNG is a static fallback for pallet tiles
    # / screenshots; the playback engine replaces it with frame-by-frame
    # compositing at slide enter (Phase 5b backend bullet — see
    # IMPLEMENTATION_PLAN). Mutually exclusive with background_image_slide_id.
    background_video_slide_id: UUID | None = None
    auto_mode: Literal["time", "date", "day"] | None = None
    auto_format: (
        Literal[
            "time_hm",  # HH:MM (24h, zero-padded)
            "time_hms",  # HH:MM:SS (24h, zero-padded)
            "date_iso",  # YYYY-MM-DD
            "date_long",  # April 21, 2026
            "date_medium",  # Apr 21
            "day_long",  # Monday
            "day_short",  # Mon
        ]
        | None
    ) = None

    # Transition INTO the next slide ("cut" = instant; "fade" = alpha-blend
    # across `transition_ms` after this slide's duration ends).
    transition: Literal[
        "cut", "fade", "wipe", "slide", "iris", "scroll", "flip", "marquee", "dissolve", "pixelate", "halftone", "scanline", "glitch", "push", "blinds", "shutter"
    ] = "cut"
    transition_ms: int = Field(default=500, ge=0, le=5000)

    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("text_color", "background_color")
    @classmethod
    def _uppercase_hex(cls, value: str) -> str:
        """Canonicalize hex colors to uppercase so `#ffaa00` and `#FFAA00`
        compare and dedupe as the same value."""
        return value.upper()

    @model_validator(mode="after")
    def _bg_layers_are_exclusive(self) -> "TextSlide":
        """A TextSlide can have one background source: solid color, an
        ImageSlide, or a VideoSlide — not two layered references at once.
        The editor's bg-picker is a radio so this combo can't be reached
        from the UI; the validator catches a malformed payload before it
        round-trips through storage."""
        if (
            self.background_image_slide_id is not None
            and self.background_video_slide_id is not None
        ):
            raise ValueError(
                "TextSlide cannot reference both an image and a video background; pick one"
            )
        return self

    @model_validator(mode="after")
    def _auto_format_matches_mode(self) -> "TextSlide":
        """auto_format is mode-scoped — a "time_hm" format can't live on
        a date slide. Catch the mismatch here so the editor can't send
        a nonsensical combo past validation."""
        if self.auto_mode is None:
            if self.auto_format is not None:
                raise ValueError("auto_format is only valid when auto_mode is set")
            return self
        prefix = self.auto_mode + "_"
        if self.auto_format is not None and not self.auto_format.startswith(prefix):
            raise ValueError(
                f"auto_format {self.auto_format!r} doesn't match auto_mode={self.auto_mode!r}"
            )
        return self


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
    transition: Literal[
        "cut", "fade", "wipe", "slide", "iris", "scroll", "flip", "marquee", "dissolve", "pixelate", "halftone", "scanline", "glitch", "push", "blinds", "shutter"
    ] = "cut"
    transition_ms: int = Field(default=500, ge=0, le=5000)

    created_at: datetime = Field(default_factory=_utcnow)


class VideoSlide(BaseModel):
    """A user-uploaded video.

    Storage layout:

        <id>/asset.png   — thumbnail (first frame, for saved-slides list)
        <id>/asset.mp4   — H.264 in MP4 container, capped at 1080p.

    The browser-side ffmpeg.wasm pipeline transcodes the operator's source
    to H.264 at min(source, 1920×1080) — the Pi Zero 2 W's hardware
    decoder tops out at 1080p30, so anything larger would fall to software
    decode and stutter. The playback engine scales down further to the
    current panel dims via ffmpeg's `-vf scale` filter inside the decode
    pipeline (sws_scale, no Python in the per-frame loop).

    `duration_ms` is informational: the playback engine reads the actual
    runtime from the file. Keeping it present so the schema parallels
    TextSlide / ImageSlide and a single ContentItem union works.
    """

    type: Literal["video"] = "video"
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(max_length=200)
    duration_ms: int = Field(default=5000, ge=100)

    # Same transition contract as TextSlide/ImageSlide — applied on the way
    # out, so a cut/fade into the next slide still works across variants.
    transition: Literal[
        "cut", "fade", "wipe", "slide", "iris", "scroll", "flip", "marquee", "dissolve", "pixelate", "halftone", "scanline", "glitch", "push", "blinds", "shutter"
    ] = "cut"
    transition_ms: int = Field(default=500, ge=0, le=5000)

    created_at: datetime = Field(default_factory=_utcnow)


# Discriminated union of content variants. Pydantic uses the `type` literal to
# route to the right subclass on deserialize.
ContentItem = Annotated[TextSlide | ImageSlide | VideoSlide, Field(discriminator="type")]
