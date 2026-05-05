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


class TextBox(BaseModel):
    """Where text renders inside a TextSlide — a rectangle in slide-relative
    fractions (0..1). Storing as fractions instead of pixels keeps the same
    slide visually consistent across rotation flips and resolution changes:
    a `{x: 0.1, y: 0.1, w: 0.9, h: 0.9}` box looks the same on a 1920×1080
    HDMI panel as on a 64×32 HUB75 grid (per SYSTEM_SPEC §5.10a).

    Validators enforce that w,h stay in [0.1, 0.9] (min so the box can't
    shrink to invisible; max so there's always a margin to grab and
    resize from) and that x+w / y+h stay inside the slide.
    """

    x: float = Field(default=0.1, ge=0.0, le=1.0)
    y: float = Field(default=0.1, ge=0.0, le=1.0)
    # Default 0.8 (was 0.9) so the centered box has 10% margin on ALL
    # four sides — qarl 2026-04-30. The clamp range still allows up to
    # 0.9 so the operator can drag wider; just the default is symmetric.
    w: float = Field(default=0.8, ge=0.1, le=0.9)
    h: float = Field(default=0.8, ge=0.1, le=0.9)

    @model_validator(mode="after")
    def _stays_inside_slide(self) -> "TextBox":
        # Use a small epsilon so float-math drift on operator drag doesn't
        # spuriously trip validation when the box snaps to the slide edge.
        eps = 1e-6
        if self.x + self.w > 1.0 + eps:
            raise ValueError(
                f"box.x ({self.x}) + box.w ({self.w}) > 1.0 — "
                f"box extends past the right edge of the slide"
            )
        if self.y + self.h > 1.0 + eps:
            raise ValueError(
                f"box.y ({self.y}) + box.h ({self.h}) > 1.0 — "
                f"box extends past the bottom edge of the slide"
            )
        return self


class TextLayer(BaseModel):
    """One independently-positioned text element on a TextSlide.

    A TextSlide carries an ordered list of these (`text_layers`); v3 of
    the on-disk schema makes layered text canonical. Layers render in
    array order — later entries draw on top of earlier ones — letting
    operators stack a headline + ticker + dynamic-time stamp on the
    same slide. Per SYSTEM_SPEC §5.10a.

    `auto_mode` marks a layer as *dynamic*: visible text is regenerated
    at playback from a device-side data source (current time, today's
    date, day-of-week). Each layer carries its own `auto_mode`/
    `auto_format` independently. The stored asset.png shows the value
    at save time; the playback engine recomposites live per-tick.

    §5.10a v3.1 (qarl 2026-05-01, accordion-editor handoff) adds a
    handful of editor-driven fields: `name` (layer label distinct from
    `text`), `motion`, `opacity`, `anchor`, `visible`, `locked`,
    `outline`, `weight`, `blend`. All are optional with sensible
    defaults — old envelopes load cleanly without a SCHEMA_VERSION
    bump (Pydantic backfills missing fields). Render-side support for
    motion / opacity / anchor / blend lands in a later wave; until
    then the renderer treats them as static / 1.0 / center / normal.
    """

    text: str = Field(max_length=10_000)
    # Operator-set short label — "Headline", "Hours", "Marquee" — that
    # the accordion editor surfaces in each layer card's header.
    # Distinct from `text`, which is the visible string. Empty string
    # → editor falls back to a generated "Layer N" placeholder.
    name: str = Field(default="", max_length=200)
    font_family: str | None = None
    font_size_px: int | None = Field(default=None, ge=4, le=2048)
    # Per §5.10a fu (qarl 2026-04-30): font_size_pct is a percentage of
    # the SLIDE height, not the box height.
    font_size_pct: float | None = Field(default=None, ge=0.5, le=100.0)
    # Optional CSS-style font weight (300–900, multiples of 100). Most
    # bundled @font-face families are single-weight; this is for the
    # variable-weight ones (Inter, Oswald, Roboto Slab, …).
    weight: int | None = Field(default=None, ge=100, le=900)
    text_color: str = Field(default="#FFFFFF", pattern=_HEX_COLOR_PATTERN)
    # Stroke outline around the glyphs. Editor-set; render support TBD.
    outline: bool = False
    # Layer opacity, 0–1. Composited by the renderer when < 1.
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    # Vertical anchor INSIDE the box. The current renderer always
    # center-anchors; editor stores this for forward-compat with
    # top/bottom layout (next render-side commit).
    anchor: Literal["top", "center", "bottom"] = "center"
    # Editor-driven hide toggle (the eye icon on each layer card).
    # Hidden layers are excluded from save-time rasterization so the
    # stored asset.png matches what the operator sees in preview.
    visible: bool = True
    # Editor-driven lock toggle. Locked layers reject text/box edits
    # in the editor; the model just carries the bit.
    locked: bool = False
    # Per-layer animation. The renderer currently honors only "static"
    # — every other value loads cleanly and survives saves but draws as
    # static until the render-side wave (see docs/text-layer-motion-
    # spec.md, step 3) lands.
    motion: Literal[
        "static",
        "ticker",   # horizontal travel, linear, LTR
        "breathe",  # scale around box center, sine
        "pulse",    # alpha modulation, sine
        "bounce",   # vertical bob, sine
        "shake",    # deterministic Gaussian micro-jitter
        "blink",    # square-wave on/off opacity
    ] = "static"
    # Operator-facing single-knob motion control (0=mild, 100=intense).
    # Each effect maps this to a sensible per-effect range — see
    # docs/text-layer-motion-spec.md for the table.
    motion_intensity: int = Field(default=50, ge=0, le=100)
    # Per-layer phase offset on the shared global tick (0.0=in-phase,
    # 0.5=opposition, 1.0=full cycle = in-phase). Lets two layers with
    # the same effect run out-of-sync without per-layer tick clocks.
    motion_phase: float = Field(default=0.0, ge=0.0, le=1.0)
    # Compositing mode against the layers below. "normal" = source-over
    # (today's behavior); the rest are reserved for the render-side
    # wave.
    blend: Literal["normal", "screen", "multiply", "overlay"] = "normal"
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
    # Where the layer renders — see TextBox + §5.10a. Defaults to a
    # centered, 10%-margin-all-around box. Resolution- and rotation-safe.
    box: TextBox = Field(default_factory=TextBox)

    @field_validator("text_color")
    @classmethod
    def _uppercase_hex(cls, value: str) -> str:
        """Canonicalize hex colors to uppercase so `#ffaa00` and `#FFAA00`
        compare and dedupe as the same value."""
        return value.upper()

    @field_validator("motion", mode="before")
    @classmethod
    def _migrate_legacy_motion(cls, value):
        """Lazy in-validator migration: rename the legacy `"scroll"`
        value to its new name `"ticker"`. Old envelopes load cleanly
        without a SCHEMA_VERSION bump (which would orphan every
        existing v3 item under storage.py's strict-equality loader);
        the new value writes back on the next save() so the rename
        drains as content gets edited. See docs/text-layer-motion-
        spec.md."""
        if value == "scroll":
            return "ticker"
        return value

    @model_validator(mode="after")
    def _auto_format_matches_mode(self) -> "TextLayer":
        """auto_format is mode-scoped — a "time_hm" format can't live on
        a date layer. Catch the mismatch here so the editor can't send
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


class BackgroundPattern(BaseModel):
    """Procedural pattern as a TextSlide background source — replaces
    BackgroundGradient (qarl 2026-05-03 designer handoff). Generalizes
    "two-stop gradient" into 11 procedural patterns, all built from
    two colors + a density knob, all rendered via PIL once at slide
    entry and baked into the bg cache (same lifecycle as solid color).

    Mutex with background_image_slide_id and background_video_slide_id
    (enforced by TextSlide's model validator).

    Reference design lives in /tmp/handoffs/generated-bg-2026-05-03/
    openmarquee-app-ui/project/app/bg-system.js — each pattern's CSS
    `build(a, b, density)` function maps 1:1 to a Pillow renderer in
    auto_render._render_pattern_*.

    `density` is a normalized 0..1 knob whose meaning is per-pattern:
      - solid:     ignored (color_b also ignored — single color)
      - gradient:  repurposed as angle (density 0 → 0deg top→bottom,
                   density 0.5 → 160deg "sunrise", density 1 → 270deg
                   right→left)
      - dots:      tile size lerp(28, 8) — higher density = tighter dots
      - halftone:  tile size lerp(36, 14) — two offset dot grids
      - stripes:   tile size lerp(40, 10) — diagonal 45deg
      - scanlines: line spacing lerp(8, 3) — horizontal CRT lines
      - checker:   tile size lerp(32, 8)
      - rings:     concentric ring spacing lerp(60, 18)
      - rays:      slice count 2*round(lerp(4, 16)) — conic A/B alternating, always even
      - confetti:  base tile lerp(60, 28) — 4 offset dot layers
      - bricks:    brick width lerp(80, 32) — staggered courses
    """

    pattern: Literal[
        "solid", "gradient", "dots", "halftone", "stripes",
        "scanlines", "checker", "rings", "rays", "confetti", "bricks",
    ]
    color_a: str = Field(pattern=_HEX_COLOR_PATTERN)
    color_b: str = Field(default="#FFFFFF", pattern=_HEX_COLOR_PATTERN)
    density: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("color_a", "color_b")
    @classmethod
    def _uppercase_hex(cls, value: str) -> str:
        return value.upper()


class TextSlide(BaseModel):
    """A user-typed text slide composed of one or more TextLayers.

    The browser renders the layered scene to a PNG at the sign's native
    resolution using Canvas, then uploads the PNG. Layer metadata is
    kept on the device so the slide can be re-opened and edited without
    losing fidelity.

    Schema v3 (qarl 2026-05-01): per-text fields (text / font_family /
    text_color / auto_mode / auto_format / box) moved off the slide
    root into `text_layers`. Slide-level fields stay at the root:
    background fill / image / video, transition, transition_ms,
    duration_ms, name. The on-disk envelope's SCHEMA_VERSION bumps to
    3 since v2 envelopes (single-box flat layout) won't deserialize
    against this shape — operators wipe their dev content directory
    once on rollout (clean cutover; no production installations to
    migrate per qarl).
    """

    type: Literal["text_slide"] = "text_slide"
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(max_length=200)
    duration_ms: int = Field(default=5000, ge=100)

    # Ordered list of text layers; index 0 draws first, later entries
    # composite over earlier ones. min_length=1 — a slide with no
    # layers is meaningless and would render blank.
    text_layers: list[TextLayer] = Field(
        default_factory=lambda: [TextLayer(text="")],
        min_length=1,
    )

    background_color: str = Field(default="#000000", pattern=_HEX_COLOR_PATTERN)
    # Optional: render a bundled / saved ImageSlide's PNG under the text.
    # The UI flattens layers + bg to a single PNG at save time, so the
    # device doesn't re-composite at playback — we keep the reference
    # for re-editing (operator clicks a saved TextSlide; the editor
    # hydrates the background picker with the right selection).
    background_image_slide_id: UUID | None = None
    # Optional: render a saved VideoSlide as the background under the
    # text. Per SYSTEM_SPEC §5.10, the device composites the cached
    # text PNG over each video frame at playback time. Mutually
    # exclusive with background_image_slide_id.
    background_video_slide_id: UUID | None = None
    # Optional: render a procedural pattern (one of 11 — gradient,
    # dots, halftone, stripes, scanlines, checker, rings, rays,
    # confetti, bricks, solid) as the background under the text.
    # Mutually exclusive with the image / video bg references. Solid
    # `background_color` field above is still kept on the model (used
    # as the implicit fallback when no pattern / image / video is
    # set; some clients fall back to it on render errors). The
    # pattern takes precedence when set.
    #
    # Replaces the prior `background_gradient` field (qarl 2026-05-03);
    # a model_validator(mode="before") below migrates legacy gradient
    # JSON in-place so existing slides keep deserializing.
    background_pattern: "BackgroundPattern | None" = None

    # Transition INTO the next slide ("cut" = instant; "fade" =
    # alpha-blend across `transition_ms` after this slide's duration
    # ends).
    transition: Literal[
        "cut", "fade", "wipe", "slide", "iris", "scroll", "flip", "marquee", "dissolve", "pixelate", "halftone", "scanline", "glitch", "push", "blinds", "shutter"
    ] = "cut"
    transition_ms: int = Field(default=500, ge=0, le=5000)

    created_at: datetime = Field(default_factory=_utcnow)
    # Mirror of the storage envelope's `updated_at`. Output-only —
    # populated by ContentStorage.load() so frontends can cachebust
    # asset URLs against this stamp.
    updated_at: datetime | None = None

    @field_validator("background_color")
    @classmethod
    def _uppercase_hex(cls, value: str) -> str:
        """Canonicalize hex colors to uppercase so `#ffaa00` and `#FFAA00`
        compare and dedupe as the same value."""
        return value.upper()

    @model_validator(mode="before")
    @classmethod
    def _migrate_gradient_to_pattern(cls, data):
        """One-shot migration of legacy `background_gradient` JSON to
        the new `background_pattern` shape (qarl 2026-05-03 handoff:
        gradient is now one of 11 patterns). Existing slides on disk
        keep deserializing.

        Field mapping:
            background_gradient.start_color → background_pattern.color_a
            background_gradient.end_color   → background_pattern.color_b
            background_gradient.angle_deg   → background_pattern.density
                                              (= angle_deg / 270, clamped 0..1)

        The 270° divisor matches the bg-system.js convention where
        `density: 0 → 0deg, 0.5 → ~135deg, 1 → 270deg` — so a
        legacy 270° gradient becomes density=1, a 135° gradient
        becomes density=0.5, and so on. Density values outside
        [0, 1] (e.g. legacy 360° gradients) clamp to 1.
        """
        if not isinstance(data, dict):
            return data
        legacy = data.pop("background_gradient", None)
        if legacy and "background_pattern" not in data:
            # Defensive float coerce — the prior BackgroundGradient
            # model required numeric angle_deg, but a hand-edited
            # JSON or a partial-write could leave it None / "auto" /
            # something. Fall back to mid-density 0.5 (≈ 135°)
            # rather than crash content storage on load.
            try:
                angle_deg = float(legacy.get("angle_deg", 0.0))
            except (TypeError, ValueError):
                angle_deg = 0.5 * 270.0  # middle of legal range
            density = max(0.0, min(1.0, angle_deg / 270.0))
            data["background_pattern"] = {
                "pattern": "gradient",
                "color_a": legacy.get("start_color", "#000000"),
                "color_b": legacy.get("end_color", "#FFFFFF"),
                "density": density,
            }
        return data

    @model_validator(mode="after")
    def _bg_layers_are_exclusive(self) -> "TextSlide":
        """A TextSlide can have one background source: solid color, an
        ImageSlide, a VideoSlide, or a procedural pattern — not two
        layered references at once. The editor's bg-picker is a radio
        so this combo can't be reached from the UI; the validator
        catches a malformed payload before it round-trips through
        storage."""
        present = sum(
            1 for v in (
                self.background_image_slide_id,
                self.background_video_slide_id,
                self.background_pattern,
            ) if v is not None
        )
        if present > 1:
            raise ValueError(
                "TextSlide background must be exactly one of: solid "
                "background_color, background_image_slide_id, "
                "background_video_slide_id, or background_pattern"
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
    # See TextSlide.updated_at — output-only mirror of the storage envelope.
    updated_at: datetime | None = None


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
    # See TextSlide.updated_at — output-only mirror of the storage envelope.
    updated_at: datetime | None = None


# Discriminated union of content variants. Pydantic uses the `type` literal to
# route to the right subclass on deserialize.
ContentItem = Annotated[TextSlide | ImageSlide | VideoSlide, Field(discriminator="type")]
