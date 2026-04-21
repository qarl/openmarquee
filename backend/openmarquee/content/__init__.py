"""Content model — typed descriptions of things that can be played on the sign.

The model is a discriminated union; every variant carries a `type` literal that
serializes/deserializes the right subclass without ambiguity. Phase 2 ships
only `TextSlide` because that's the F&F demo path; `Image` and `Video` are
added as their respective UI features land (post-demo).

What's deliberately *not* in the model: where any rendered asset lives on disk.
That's the storage layer's job (`openmarquee.content.storage`, lands in the
next commit). Models are pure metadata; storage maps an item's `id` to bytes.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

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

    created_at: datetime = Field(default_factory=_utcnow)

    @field_validator("text_color", "background_color")
    @classmethod
    def _uppercase_hex(cls, value: str) -> str:
        """Canonicalize hex colors to uppercase so `#ffaa00` and `#FFAA00`
        compare and dedupe as the same value."""
        return value.upper()


# Today this is a type alias for the only content variant we support. Once
# `Image` and `Video` land, this becomes a proper discriminated union:
#
#     ContentItem = Annotated[
#         TextSlide | Image | Video,
#         Field(discriminator="type"),
#     ]
#
# The `type` literal on each variant is already in place to make that switch a
# one-line change.
ContentItem = TextSlide
