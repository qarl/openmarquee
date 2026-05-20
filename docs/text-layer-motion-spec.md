# Text-Layer Motion Spec

**Status:** shipped — see [`motion.py`](../backend/openmarquee/motion.py) (motion effects + compose_motion_frame; sweep #2 perf work in Batch 8.4 added the scratch buffer pool).

Drafted 2026-05-02 after the
DRM/KMS rewrite (commit chain fc433dc → a55a215) collapsed the
per-frame render cost to ~0.5 ms on Pi Zero 2 W, making per-tick
animation of text layers cheap enough to design in.

The existing `motion: Literal["static", "scroll", "pulse"]` field on
`TextLayer` (`backend/openmarquee/content/__init__.py:125`) was a
forward-compat placeholder — the editor stores the value but the
renderer treats every layer as static. This doc nails down the
operator-facing menu, semantics, and per-effect performance budget so
the render-side wave can land.

---

## Motion values

Seven enum values (six effects + `static`):

| value     | category | what it does |
|-----------|----------|--------------|
| `static`  | none     | no animation (default; current behavior) |
| `ticker`  | translate | tiled marquee — the text repeats every box-width and scrolls left continuously, so the box always shows text |
| `breathe` | scale    | text grows and shrinks rhythmically around the box center |
| `pulse`   | alpha    | text fades up and down in opacity |
| `bounce`  | translate | text bobs vertically inside the box |
| `shake`   | translate | text micro-jitters (small random translates per frame) for emphasis |
| `blink`   | alpha    | hard on/off opacity at a fixed cadence |

`marquee`, `scroll`, `crawl`, `strobe`, `wave`, `glow`, `breathe`-as-
alpha, and `color-cycle` were considered and rejected during scoping
(2026-05-02 chat with qarl). Notable rejections:

- **`marquee`** name-collides with the existing slide-to-slide
  transition of the same name (`playlist.py:101` + `_marquee()` in
  `playback.py`). Two dropdowns saying "Marquee" with different
  meanings would be operator-hostile. `ticker` carries the same idiom
  without the collision.
- **`wave`** (sine-distort each glyph column vertically) was
  dropped without benching: the vectorized numpy approach should be
  ~3-8 ms/frame at 1080p sign-native (fancy-index over a 700×200
  RGBA bitmap, memory-bandwidth bound on LPDDR2-450), which fits 30
  fps but uses real budget and the visual payoff was judged not
  worth it for now.
- **`color-cycle`** (hue rotate over time) was deferred — it's
  cheap to implement (~1-2 ms per-frame numpy hue shift) but has
  design implications (text legibility, brand-color violation) that
  need separate thought.

## Box-bounded semantics (the universal constraint)

**All animation occurs relative to the text layer's `box`.** The box
is the layer's authoritative coordinate frame:

- `ticker` translates inside the box; text clipped at box edges
- `breathe` scales around the **box center** (not the glyph bbox
  center — see below)
- `bounce` bobs within the box's vertical extent
- `shake`'s jitter offsets are clipped against box edges

This means the operator can place an animated layer in a sub-region
of the sign and the animation stays inside that region. Multiple
animated layers in different boxes don't visually interfere.

### `breathe` pivot

Scaling pivots on the **box center**, not the glyph bbox center.
The two are usually identical (text centered in box) but if the
operator deliberately offset the text within the box (e.g. nudged it
toward the top-left corner), the offset must be preserved during
scaling. Math:

- Find the unscaled glyph bbox center `(gx, gy)` relative to box
  origin
- Offset-from-box-center: `(dx, dy) = (gx - box_cx, gy - box_cy)`
- At breathe scale `s`, paste position = `(box_cx + s*dx,
  box_cy + s*dy)` minus half the scaled bbox dims

Centered text → `(dx, dy) = (0, 0)` → stays centered through the
breathe cycle. Off-center text → orbits outward/inward around the
box center, which preserves operator intent.

### `breathe` perf trick: render at glyph bbox, not box

Pre-render the text at its glyph bounding box (Pillow's
`image.getbbox()` after `ImageDraw.text`), not at full box
dimensions. Typical glyph bbox is 30-50 % of box area; resizing
that smaller bitmap per frame is proportionally cheaper. At 1080 p
sign-native the resize cost drops from ~10-20 ms to ~1-2 ms.

## Per-frame cost on Pi Zero 2 W

All numbers post-DRM/KMS rewrite (commits 097be7b through 01636b4).
Render budget at 30 fps = 33 ms.

| sign res     | per-frame motion work, single layer   | budget % |
|--------------|---------------------------------------|----------|
| 128 × 96     | ticker / breathe / pulse / blink: <1 ms | <3 %    |
| 128 × 96     | bounce / shake: <500 µs               | <2 %    |
| 1080 p native | ticker (paste a row of pixels): ~2 ms  | ~6 %    |
| 1080 p native | breathe (resize cropped glyph bbox): ~1-2 ms | ~6 % |
| 1080 p native | pulse / blink (alpha multiply): ~3 ms  | ~9 %    |
| 1080 p native | bounce / shake (translate paste): ~3 ms | ~9 %    |

At 128 × 96 sign + HVS upscale (the dev Pi config), all six effects
fit easily, even with multiple layers stacked. At 1080 p sign-native
the budget is real but accommodates 3-4 simultaneously animated
layers comfortably.

## HVS plane-scaling shortcut for `breathe`

For the **single most prominent** animated layer, `breathe` can
bypass per-frame software resize entirely:

1. Render the text once at the box's max-pulse-size into the overlay
   plane buffer
2. Per frame, compute the new CRTC dest rect for the overlay plane
   and submit an atomic commit changing only `CRTC_X/Y/W/H` — the
   buffer doesn't change

Per-frame cost: ~0.5 ms (atomic ioctl), independent of sign
resolution. Quality: the vc4 HVS does sub-pixel scaling, smoother
than software resize. Constraint: there's only one overlay plane
(2a-2 only allocates one), so this is a single-layer optimization.
Multiple `breathe` layers fall back to software resize on the
software-composited overlay.

## Schema migration

Today's `motion` field:

```python
motion: Literal["static", "scroll", "pulse"] = "static"
```

Becomes:

```python
motion: Literal[
    "static", "ticker", "breathe", "pulse",
    "bounce", "shake", "blink",
] = "static"
motion_intensity: int = Field(default=50, ge=0, le=100)
motion_phase: float = Field(default=0.0, ge=0.0, le=1.0)
```

**No `schema_version` bump.** The current `ContentStorage.load()`
loader (`content/storage.py:168`) does strict-equality on
`schema_version` and *rejects* mismatched envelopes — bumping
v3 → v4 would render every existing item unloadable until a
migration sweep rewrote them, which is a production-data hazard
qarl shouldn't pay for an additive change. The migration is
therefore designed to be additive-only and version-stable:

- New fields (`motion_intensity`, `motion_phase`) have defaults, so
  Pydantic populates them on load when absent. Old envelopes
  validate cleanly. **No `SCHEMA_VERSION` constant change in
  `storage.py`.**
- The `"scroll"` → `"ticker"` rename is handled by a Pydantic
  `field_validator(mode="before")` on `TextLayer.motion` that maps
  `"scroll"` → `"ticker"` before the `Literal` validates. Old
  envelopes with `"scroll"` produce in-memory layers with
  `motion="ticker"`, and the next `save()` writes the new value
  back, so the migration drains lazily as content gets edited.
- Old envelopes with `"pulse"` load as-is. **Important:** today's
  `"pulse"` was specced as alpha modulation but never actually
  rendered (the renderer always treated it as static). Operators
  who configured `"pulse"` expecting scaling will silently get
  alpha behavior in v1. The editor should surface the new
  `breathe` option clearly so operators can re-pick if their
  intent was scale.

Schema version stays at 3. If a future change requires a real
breaking migration, the bump strategy is: extend the loader to
accept `version <= SCHEMA_VERSION` and run a per-version migration
chain — but that's out of scope for this spec.

### What ships in step 1

A single PR touching `backend/openmarquee/content/__init__.py`:

- Extend `TextLayer.motion`'s `Literal` to the seven new values.
- Add `motion_intensity` (`int`, `0-100`, default `50`).
- Add `motion_phase` (`float`, `0.0-1.0`, default `0.0`).
- Add the `field_validator` mapping `"scroll"` → `"ticker"` before
  validation.
- Unit tests: load an old envelope with `motion="scroll"` and assert
  the loaded item has `motion="ticker"`; load an envelope without
  the two new fields and assert the defaults are populated.

No renderer changes, no loader changes, no schema_version touched.

## Decisions locked from QA review (2026-05-02)

QA review pass on this spec landed five answers:

1. **Single shared `motion_intensity` (0-100)** for v1. Per-effect
   knobs deferred — paralysis-of-choice at v1.
2. **Default stays `static`.** Animated-by-default reads as gimmick.
3. **CSS keyframes editor preview is fine.** Pixel-identical
   editor↔device parity is over-engineering at this stage.
4. **Shared global tick.** Independent clocks drift visibly.
5. **`motion_phase` (0-1) schema field — yes.** Cheap, "free until
   discovered" operator-side; enables "wave-of-pulses" without
   later schema churn.

Schema therefore grows two fields, not one:

```python
motion: Literal[
    "static", "ticker", "breathe", "pulse",
    "bounce", "shake", "blink",
] = "static"
motion_intensity: int = Field(default=50, ge=0, le=100)
motion_phase: float = Field(default=0.0, ge=0.0, le=1.0)
```

## Per-effect intensity=50 defaults (proposed)

QA flagged that intensity is meaningless without per-effect mappings.
Proposed mapping at intensity=50 (the schema default):

| effect    | intensity=50 means                                                | range over 0-100 |
|-----------|-------------------------------------------------------------------|------------------|
| `ticker`  | full text-width travel cycle per ~3 s                             | ~6 s slow → ~1 s fast |
| `breathe` | ±10 % scale around 100 % at 1 Hz                                  | ±2 % → ±20 % |
| `pulse`   | alpha sweeps 30 %→100 % at 1 Hz                                   | 70-100 % shallow → 0-100 % deep |
| `bounce`  | ±5 % of box height at 1 Hz                                        | ±1 % → ±10 % |
| `shake`   | ±2 % of glyph height random-walk at ~10 Hz                        | ±0.5 % → ±4 % |
| `blink`   | 1 Hz on/off (square wave, 50 % duty)                              | 0.5 Hz slow → 4 Hz fast |

Period stays roughly constant across intensity for `ticker` /
`breathe` / `pulse` / `bounce` / `blink`; `shake` modulates amplitude
not frequency. All effects derive their wall-clock phase from a
single shared monotonic clock (Q4 lock above) plus the layer's
`motion_phase` offset, so two `breathe` layers with `motion_phase=0`
and `motion_phase=0.5` will be in opposition.

### Curve shapes

To remove the ambiguity that two implementations might pick different
waveforms:

- `breathe`, `pulse`, `bounce` — **sine** of the shared clock
  (smooth, no implementation-defined easing differences).
- `blink` — **square** wave, 50 % duty (already specified above).
- `ticker` — **linear** travel of a TILED marquee: the text is
  drawn repeated every box-width and scrolls left continuously, so
  the box always shows text (no single-copy gap). Density-parity
  rewrite (2026-05-20): the device renderer now matches the Canvas
  editor ticker's two-copy tiling wrap at a 1×-box-width repeat
  pitch — so for `ticker` the device/editor match is density-exact,
  not the "visually approximate" the Q3 lock grants the CSS-keyframe
  effects.
- `shake` — **per-frame Gaussian** translate offsets, seeded
  deterministically per layer (see below).

### `shake` randomness

`shake` is the only effect whose visual depends on RNG. To keep it
deterministic across reloads (matching the spirit of the phase=0
slide-load rule):

- The RNG is seeded from a hash of the layer id + the layer's
  `motion_phase`. Same layer + same phase = same shake sequence.
- Different layers produce different sequences (so multi-layer
  shake doesn't look mechanically identical).
- The RNG advances on the shared global tick, not on wall clock —
  so frame drops don't desync the shake pattern across the device
  and editor preview.

## Per-effect specifics raised in QA review

- **`ticker` direction** — LTR for v1 (text enters from right edge,
  exits left). RTL is a follow-up if a customer asks. Keeping it
  fixed avoids a third schema field and matches the dominant Western-
  reading expectation.
- **`shake` amplitude clamp** — capped at 4 % of glyph height
  (intensity=100). At 128 × 96 sign with 30-px text, that's ~1 px
  max — visible but not pixelated. At 1080 p with 200-px text, ~8 px
  — emphasis without disintegration.
- **Cycle starting phase on slide load** — deterministic, phase=0
  + `motion_phase` (no random init). Predictable across reloads;
  transition-into-animated-slide always begins at the same visual
  state for a given layer.

## Performance bench — outstanding

QA push-back: numbers in the per-frame cost table are extrapolations
from the post-2a-3 render path, NOT benched against actual motion
code. Before promising 30 fps with 3-4 simultaneously animated
layers at 1080 p sign-native, micro-bench at least `bounce` (cheapest
layout-changing op) on the Pi. **Deferred until next dev-Pi
session.** At 128 × 96 sign + HVS upscale (current dev Pi config) the
budget headroom is so large the bench isn't a blocker for v1.

## Implementation order

If the spec is approved, suggested order:

1. **Schema migration** (this spec's deliverable). Extend the
   `motion` Literal, add `motion_intensity` + `motion_phase` with
   defaults, register the `"scroll"` → `"ticker"` field_validator.
   No renderer changes, no loader changes, no `schema_version`
   bump. Lands cleanly.
2. **Editor support** — extend the editor's motion dropdown to all
   seven values + intensity slider + phase slider. Editor preview
   uses CSS keyframes (Q3 lock above) — visually approximate, not
   pixel-identical.
3. **Renderer — software path** — implement all six effects as a
   per-tick callback in `playback.py` that re-composites the
   relevant layer into the overlay buffer. Lands all six effects
   working at sign-native dims.
4. **Renderer — HVS shortcut for `breathe`** — opt-in optimization
   that picks the most prominent `breathe` layer and routes it to
   per-frame atomic commits instead of software resize. Quality +
   perf win on the dev Pi.
5. **Per-effect timing knobs** — once 1-4 are in operator hands and
   QA has feedback, iterate on the schema.

### Step 1+2 ship a known dead-end UX

Between step 2 (editor exposes the new effects) and step 3 (renderer
honors them), an operator who picks `motion=bounce` or any of the
new values sees the editor preview animating but the device
displaying static text. This is **acceptable** — both steps are
small, ship close together, and the editor's CSS preview
correctly tells the operator what the device WILL do. No feature
flag is added to gate the editor; the brief gap is the cost of
shipping the migration before the renderer to keep PRs small.

### Performance bench gate

The 1080 p sign-native cost numbers are extrapolated from the
post-2a-3 render path; not benched against actual motion code. The
bench is **not a v1 ship blocker** at the dev Pi's 128 × 96 sign +
HVS-up config (where headroom is enormous), but **is a v1 ship
blocker if the codebase ever ships content rendered at 1080 p
sign-native**. Pin the bench to step 3 (renderer software path) on
the dev Pi as a verification step before declaring step 3 done.
