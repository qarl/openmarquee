"""Content model — typed descriptions of things that can be played on the sign.

Every variant carries a `type` literal, and `ContentItem` is a discriminated
union over them. Pydantic picks the right subclass from the `type` field on
the wire.

What's deliberately *not* in the model: where any rendered asset lives on disk.
That's the storage layer's job (`openmarquee.content.storage`). Models are pure
metadata; storage maps an item's `id` to bytes.

Variants today: `TextSlide`, `ImageSlide`, `VideoSlide`, `StreamSlide`,
`WebSlide`.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

# Hex color regex: #RRGGBB. Six lowercase or uppercase hex digits.
_HEX_COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"

#: URL schemes a WebSlide may point at. A Web slide renders an actual
#: web page, so only the two HTTP transports are allowed — unlike the
#: stream allowlist (rtsp/rtmp/srt/udp/...) this is deliberately just
#: `http`/`https`. Everything else (`file:`, `ftp:`, a bare path, ...)
#: is rejected by `validate_web_url`.
ALLOWED_WEB_SCHEMES = frozenset({"http", "https"})


def validate_web_url(url: str) -> None:
    """Raise `ValueError` if `url` is not a plain `http`/`https` web page.

    The URL is operator-supplied and is handed to the render helper
    (`web-helper/`) which loads it in a real browser. A WebSlide must
    point at an actual web page, so the scheme allowlist is exactly
    `{"http", "https"}` — `file:`, `ftp:`, `data:` and friends are
    rejected (a `file:` URL would turn an operator-typed field into a
    local-file-read primitive on the helper machine).

    This intentionally does NOT reuse `stream_consumer.validate_stream_url`
    — that allows the rtsp/rtmp/srt/udp transports, which are nonsense
    for a web page. Pure function: returns None on success.

    The scheme comparison is case-insensitive. A URL with no scheme at
    all (a bare path like `/etc/passwd`), an empty string, or a URL
    carrying ASCII control characters (a header-injection / smuggling
    vector) is rejected.
    """
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise ValueError("web URL must not contain control characters")
    scheme = urlparse(url).scheme.lower()
    if scheme not in ALLOWED_WEB_SCHEMES:
        allowed = ", ".join(sorted(ALLOWED_WEB_SCHEMES))
        shown = scheme if scheme else "(none)"
        raise ValueError(
            f"web URL scheme {shown!r} is not allowed; "
            f"the URL must use one of: {allowed}"
        )


def _utcnow() -> datetime:
    """Timezone-aware UTC now. (datetime.utcnow is deprecated in 3.12+.)"""
    return datetime.now(UTC)


class TextBox(BaseModel):
    """Where text renders inside a TextSlide — a rectangle in slide-relative
    fractions. Storing as fractions instead of pixels keeps the same slide
    visually consistent across rotation flips and resolution changes: a
    `{x: 0.1, y: 0.1, w: 0.8, h: 0.8}` box looks the same on a 1920×1080
    HDMI panel as on a 64×32 HUB75 grid (per SYSTEM_SPEC §5.10a).

    Bounds (qarl 2026-05-19, schema-widen for animated entrance/exit +
    edge-peeking captions):
      x, y ∈ [-2.0, 3.0]: the box origin can sit up to 2x slide widths
        / heights off the edge for animated slide-ins / partial reveals
        the renderer clips off-canvas portions at draw time (GL viewport
        clipping handles it without crashing).
      w, h ∈ [0.01, 5.0]: minimum prevents zero-width degenerates;
        maximum supports oversized boxes that intentionally extend
        beyond the viewport.

    Defaults stay at (0.1, 0.1, 0.8, 0.8) — symmetric 10% margin on
    all four sides — so the editor-default centered box is unchanged.

    NOTE: pre-2026-05-19 versions enforced a `_stays_inside_slide`
    model_validator (x+w <= 1.0, y+h <= 1.0). That validator is now
    intentionally absent — off-canvas placement is a feature, not an
    error.
    """

    x: float = Field(default=0.1, ge=-2.0, le=3.0)
    y: float = Field(default=0.1, ge=-2.0, le=3.0)
    w: float = Field(default=0.8, ge=0.01, le=5.0)
    h: float = Field(default=0.8, ge=0.01, le=5.0)


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
    # Per §5.10a v3.1.2 (qarl 2026-05-01 review #3, refining ask #1):
    # font_size_pct is a percentage of BOX WIDTH. Resizing the box
    # visibly resizes the text -- operators expected box-resize ->
    # text-resize. Earlier same day went % of slide width; the review
    # correction pinned it to box width because it gives operators
    # the layout intent they're already used to.
    font_size_pct: float | None = Field(default=None, ge=0.5, le=100.0)
    # Optional CSS-style font weight (300–900, multiples of 100). Most
    # bundled @font-face families are single-weight; this is for the
    # variable-weight ones (Inter, Oswald, Roboto Slab, …).
    weight: int | None = Field(default=None, ge=100, le=900)
    text_color: str = Field(default="#FFFFFF", pattern=_HEX_COLOR_PATTERN)
    # Horizontal alignment within the box. Default center matches the
    # historical centered render. left/right introduced for B4 word-wrap
    # support (2026-05-05) -- multi-line wrapped text reads better
    # left-aligned for prose, center stays the default for short labels.
    text_align: Literal["left", "center", "right"] = "center"
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
    # Frequency multiplier for the effect's cycle. 1.0 = spec default,
    # 0.0 = frozen, 2.0 = double speed (B2, 2026-05-05). Multiplies
    # _effect_freq's per-effect Hz. Capped at 2.0 -- ticker beyond that
    # would scroll faster than the eye can track at typical font sizes.
    motion_speed: float = Field(default=1.0, ge=0.0, le=2.0)
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
      - dots:      tile size lerp(48, 4) — higher density = tighter dots
      - halftone:  tile size lerp(60, 6) — two offset dot grids
      - stripes:   tile size lerp(80, 4) — diagonal 45deg
      - scanlines: line spacing lerp(16, 2) — horizontal CRT lines
      - checker:   tile size lerp(60, 4)
      - rings:     concentric ring spacing lerp(120, 6)
      - rays:      slice count 2*round(lerp(2, 24)) — conic A/B alternating, always even
      - confetti:  base tile lerp(100, 14) — 4 offset dot layers
      - bricks:    brick width lerp(140, 16) — staggered courses
      - grid:      cell size lerp(120, 4) — color_a lines on color_b paper
    """

    pattern: Literal[
        "solid", "gradient", "dots", "halftone", "stripes",
        "scanlines", "checker", "grid", "rings", "rays", "confetti", "bricks",
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


class StreamSlide(BaseModel):
    """A scheduled video stream — a playlist slide that plays a live
    network video stream the operator is publishing
    (docs/STREAM_VLC_PROPOSAL.md §6).

    Unlike VideoSlide there is NO stored asset.mp4: the video bytes
    arrive live over the network at playback time. The only stored file
    is a synthetic asset.png thumbnail card, generated at save time (see
    content/storage.py) so the slide tile has something to show in the
    editor's saved-slides list — the renderer never paints that PNG.

    `duration_ms` is the fixed slot length, like every other slide type
    (proposal Q8 default — a "play until the stream ends" option is a
    later follow-up). `on_unreachable` (proposal Q12) decides what the
    playback loop does when the stream URL can't be reached when the slot
    fires.
    """

    type: Literal["stream"] = "stream"
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(max_length=200)
    # The stream URL the operator publishes
    # (e.g. rtsp://laptop:8554/live).
    stream_url: str = Field(max_length=2000)
    # Fixed slot length, capped at 24h.
    duration_ms: int = Field(default=10_000, ge=100, le=24 * 60 * 60 * 1000)
    # Fallback when the RTSP URL is unreachable at slot-fire time.
    on_unreachable: Literal["hold_last_frame", "black", "skip"] = (
        "hold_last_frame"
    )

    # Same transition contract as the other slide types.
    transition: Literal[
        "cut", "fade", "wipe", "slide", "iris", "scroll", "flip", "marquee", "dissolve", "pixelate", "halftone", "scanline", "glitch", "push", "blinds", "shutter"
    ] = "cut"
    transition_ms: int = Field(default=500, ge=0, le=5000)

    created_at: datetime = Field(default_factory=_utcnow)
    # See TextSlide.updated_at — output-only mirror of the storage envelope.
    updated_at: datetime | None = None


class WebSlide(BaseModel):
    """A web page rendered onto the sign — a playlist slide that shows
    a screenshot of an operator-supplied URL.

    The Raspberry Pi can't run a browser, so a render helper
    (`web-helper/`, running on the operator's machine) loads the page
    and produces screenshots; the sign fetches them. Architecturally
    the Web slide is "an image slide whose asset.png is auto-refreshed
    from the helper" — so unlike StreamSlide it DOES carry a stored
    asset.png that the renderer paints. On create there is no
    screenshot yet, so the initial asset is a synthetic placeholder
    card (see content/storage.py); the periodic-fetch producer
    overwrites it with a real screenshot.

    `duration_ms` is the fixed slot length, like every other slide
    type. `refresh_interval_s` is how often the helper re-fetches a
    fresh screenshot of `url`.
    """

    type: Literal["web"] = "web"
    id: UUID = Field(default_factory=uuid4)
    name: str = Field(max_length=200)
    # The web page the render helper screenshots
    # (e.g. https://status.example.com).
    url: str = Field(max_length=2000)
    # How often the screenshot is re-fetched, in seconds. Bounded:
    # min 10s (a tighter cadence would hammer the helper / the target
    # site) and max 24h (a day-stale screenshot is the loosest sane
    # refresh). Default 300s = a 5-minute refresh.
    refresh_interval_s: int = Field(
        default=300, ge=10, le=24 * 60 * 60
    )
    # Fixed slot length, capped at 24h — same as StreamSlide.
    duration_ms: int = Field(default=10_000, ge=100, le=24 * 60 * 60 * 1000)

    # Same transition contract as the other slide types.
    transition: Literal[
        "cut", "fade", "wipe", "slide", "iris", "scroll", "flip", "marquee", "dissolve", "pixelate", "halftone", "scanline", "glitch", "push", "blinds", "shutter"
    ] = "cut"
    transition_ms: int = Field(default=500, ge=0, le=5000)

    created_at: datetime = Field(default_factory=_utcnow)
    # See TextSlide.updated_at — output-only mirror of the storage envelope.
    updated_at: datetime | None = None


# Discriminated union of content variants. Pydantic uses the `type` literal to
# route to the right subclass on deserialize.
ContentItem = Annotated[
    TextSlide | ImageSlide | VideoSlide | StreamSlide | WebSlide,
    Field(discriminator="type"),
]
