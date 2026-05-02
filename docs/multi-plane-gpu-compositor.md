# Multi-Plane GPU Compositor — Design

Status: **proposed, awaiting qarl review**. Drafted 2026-05-02 after
the office-network return + the 1080p motion bench (4ec363c) + the
vc4 plane probe (4ec363c) confirmed that the canonical render path
needs to be GPU-side at 1080p, not CPU-side.

Reference memory: `project_hdmi_1080p_is_primary_target.md` —
HDMI 1080p TVs are the primary deployment target; LED matrices are
the rare case; software-composite at 1080p blows the 30 fps budget
by 4-6×.

---

## Motivation

`compose_motion_frame` is a software compositor. Each frame it
walks every visible layer, applies a motion transform, alpha-
composites onto the slide canvas, returns one big RGB frame. The
frame goes through `renderer.render_frame(bytes)` to one DRM plane.

At 128×96 sign-native this is fast (~1-4 ms / frame). At 1080p
sign-native it's 140-180 ms / frame — 5× over the 30 fps budget.
Why: PIL `alpha_composite` of two 1920×1080 RGBA buffers is per-
pixel arithmetic over 2 M pixels, single-threaded, no SIMD-saturated
path, ~50-100 ms per layer per frame on a Pi Zero 2 W core.

The DRM/KMS rewrite (Phase 2a-1/2/3, fc433dc → a55a215) moved the
SCALING to the GPU (HVS plane-scaling at scanout). It did NOT move
the COMPOSITING. That's the next move.

vc4 exposes 16-32 overlay planes per CRTC (probed via
`scripts/phase6_vc4_probe.py`), each with independent FB_ID,
geometry (CRTC_X/Y/W/H), alpha, zpos, blend mode, scaling filter.
That's enough plane budget to put every text layer on its own
overlay plane with 5-10× headroom. Per-frame compositing happens
at scanout in vc4's HVS hardware — zero per-pixel CPU work.

## Architecture summary

Per slide, allocate this stack of DRM planes:

```
+--------------------------------------------------+
| zpos N      = animated text layer N (plane N)   | ←—— per-frame atomic commit
| ...                                             |     changes geometry/alpha/etc
| zpos 1      = static-text overlay (plane S)     | ←—— pre-composited at slide entry,
|                                                 |     scanned out unchanged for slide lifetime
| zpos 0      = primary (background image/color)  | ←—— same as today's primary plane
+--------------------------------------------------+
```

- **Background** stays on the primary plane (today's behavior).
- **All static text layers** → pre-composited via software at slide
  entry into ONE shared "static-text" overlay plane (qarl's
  insight, 2026-05-02). One software composite per slide entry,
  not per frame.
- **Each animated text layer** → its own overlay plane, with
  its pre-rasterized RGBA bitmap. Per-frame motion is one atomic
  ioctl changing CRTC_X/Y/W/H or alpha or visibility. Zero per-
  pixel CPU per frame.

Per-frame CPU cost on the hot path:
- One atomic ioctl with N×{CRTC_X, CRTC_Y, CRTC_W, CRTC_H, alpha,
  CRTC_ID} (24 bytes per plane × N planes ≈ 200 bytes)
- Phase computation for each animated layer (sin / linear / RNG —
  microseconds total)
- No buffer copies, no compositing, no rasterization

vc4's HVS does the alpha blend + scaling at scanout. GPU does the
work, CPU does almost nothing.

## Effect → property mapping

| effect  | what changes per frame                              | property |
|---------|-----------------------------------------------------|----------|
| ticker  | translate within box, wrap                           | CRTC_X   |
| breathe | scale around box center (sin)                        | CRTC_W, CRTC_H, CRTC_X, CRTC_Y |
| pulse   | alpha modulation (sin)                               | alpha (0-65535) |
| bounce  | vertical translate inside box (sin)                  | CRTC_Y   |
| shake   | deterministic Gaussian micro-jitter                  | CRTC_X, CRTC_Y |
| blink   | hard on/off (square wave 50% duty)                   | alpha (0 or 65535) |

For ticker, the wrap requires DRAWING the text twice in the source
fb (one copy at x=0, one copy at x=text_width+gap). The plane's
CRTC_W spans 2× text width and the source rect window slides
horizontally via SRC_X. That keeps everything in atomic-commit-only
land (no per-frame buffer write).

For breathe with off-center text, we follow the spec's "scale
around box center" rule: as CRTC_W shrinks, also shift CRTC_X to
keep the box-center-of-mass stationary while the glyph orbits
inward.

## API surface (DRMRenderer extension)

```python
class DRMRenderer:
    def __init__(
        self,
        width, height,                  # SIGN dims (= display dims for HDMI 1080p)
        *,
        max_animated_planes: int = 8,   # how many overlay planes to reserve
        ...
    ):
        # Allocates: 1 primary + 1 static-text overlay + max_animated_planes
        # animated overlays. All overlays start with FB_ID=0 / CRTC_ID=0 →
        # disabled until set_animated_layer() attaches them.

    # ---- slide-entry hooks (called once per slide) ----

    def set_static_text_bitmap(self, rgba_bytes: bytes) -> None:
        """Write the pre-composited static-text RGBA into the static-
        text overlay plane's buffer. Called once per slide-entry by
        the orchestrator after software-composite of all static
        layers."""

    def attach_animated_layer(
        self, slot: int, rgba_bytes: bytes, *, max_dims: tuple[int, int]
    ) -> None:
        """Allocate (or reuse) the animated overlay plane at `slot`,
        write the layer's RGBA bitmap (sized to `max_dims` so motion
        can scale up to that without re-upload). Plane stays disabled
        (CRTC_ID=0) until the first update_animated_layer call."""

    def detach_animated_layer(self, slot: int) -> None:
        """Disable plane at `slot` (CRTC_ID=0) for the next slide.
        Called on slide-exit. The buffer can be reused on the next
        attach for a different slot."""

    # ---- per-frame hot path ----

    def update_animated_layer(
        self,
        slot: int,
        *,
        crtc_x: int | None = None,
        crtc_y: int | None = None,
        crtc_w: int | None = None,
        crtc_h: int | None = None,
        src_x: int | None = None,           # 16.16 fp; for ticker wrap
        src_w: int | None = None,           # 16.16 fp
        alpha: int | None = None,           # 0-65535
        zpos: int | None = None,
        visible: bool | None = None,        # toggles CRTC_ID 0 ↔ live
    ) -> None:
        """Stage property changes for the next commit. None = leave
        unchanged. Lazy: only properties that actually changed since
        the last commit get included in the atomic ioctl payload."""

    def commit(self) -> None:
        """One atomic ioctl with all staged property changes across
        all planes. Resets the staging buffer for the next frame."""
```

Per-frame cost: one `commit()` call → one atomic ioctl. The kernel
hands the changes to vc4's HVS for the next scanout. No per-pixel
work anywhere in the loop.

## Slide orchestrator

A new class `GPUSlideCompositor` (or freestanding functions) drives
the renderer per slide:

```python
class GPUSlideCompositor:
    def attach_slide(self, slide: TextSlide, renderer: DRMRenderer):
        """At slide entry:
        1. Classify each visible layer as static (motion=='static')
           or animated (motion in spec's effect set).
        2. Render every static layer into a single slide-sized RGBA
           bitmap via existing seed.py / motion.py rasterize helpers,
           push it to renderer.set_static_text_bitmap().
        3. For each animated layer, rasterize its bitmap (at the
           layer's box dims) and renderer.attach_animated_layer(
           slot, rgba_bytes, max_dims=...).
        4. Initialize each animated plane's geometry to its at-rest
           CRTC rect (the layer's box, in display pixels), alpha to
           65535, zpos to the layer's array-index-among-animated.
        5. renderer.commit() to flip everything live.
        Total: ~10-30 ms one-time cost at slide entry (1080p
        rasterizes are ~5-10 ms each)."""

    def tick(self, slide: TextSlide, elapsed_s: float, now: datetime | None):
        """Per-tick (30 Hz): for each animated layer, compute motion
        phase, derive property deltas, call renderer.update_animated
        _layer for each. One renderer.commit() at the end. No per-
        pixel work."""

    def detach_slide(self, slide: TextSlide, renderer: DRMRenderer):
        """At slide exit: detach every animated plane (CRTC_ID=0).
        The static-text plane gets overwritten by the next slide's
        attach."""
```

Auto-mode layers: re-rasterize per tick same as the current
software path, but write the new bytes into the layer's already-
attached plane buffer instead of recomposing the whole slide. That
keeps clock/date/day slides cheap (one mmap write per second of
the auto field's update cadence).

## Plane-budget overflow fallback

vc4 exposes 16-32 overlays per CRTC; typical slides have 1-3
animated layers. Slides with more animated layers than `max_animated
_planes` fall back: the overflow animated layers get software-
composited into the static-text plane each tick (i.e. the static-
text plane is no longer "static for the slide's lifetime" — it gets
re-rasterized when overflow layers exist). Cost = current
compose_motion_frame cost for those overflow layers only; the rest
still run on planes. Graceful degradation.

For v1, set `max_animated_planes = 8` (covers any plausible slide,
well under vc4's 16-overlay minimum). The overflow path lives but
won't fire on real slides.

## Static-text plane lifecycle

The "static-text plane is pre-composited once" promise depends on
the slide's static-layer set being stable for the slide's duration.
Cases that could invalidate it:

- **Auto-mode layer flagged as static**: shouldn't happen — auto
  IS animated content, classify auto-mode layers as animated even
  if motion=="static".
- **Editor live-edit during preview**: the inline-preview's
  drawTextSlideAnimated path (71e513f) still goes through
  compose_motion_frame; the GPU compositor is for the device
  renderer's playback path. Editor preview keeps its current
  software path.

## Open questions for qarl

1. **`max_animated_planes` default** — 8 covers any realistic slide
   with 5× margin. Lower (4) saves 32 MB at 1080p RGBA. Higher (16)
   eliminates the overflow case entirely. Pick.
2. **Static-text plane allocation** — ~~even slides with zero static
   layers allocate the static-text plane (it stays empty / disabled).
   That's 8 MB held idle at 1080p. Acceptable, or skip the
   allocation for animated-only slides?~~ **Resolved 2026-05-02**:
   the static-text plane was dropped entirely in step 1 — bg + all
   static layers software-composite into the *primary* plane at
   slide entry instead. See "Step 1 implementation notes" below.
3. **Editor preview** — keep on the software compose path
   (b453059), or also route through a JS port of the GPU compositor
   plan? Software path is fine on the operator's laptop (real
   browser, real GPU); device-path-vs-editor-path divergence is
   visible only as approximate motion timing. I'd keep the divergence
   per the spec's Q3 lock ("CSS keyframes preview is fine; pixel-
   identical = over-engineering").
4. **Z-order vs array order** — today's software path renders
   layers in `text_layers` array order (index 0 first, later
   composited on top). The GPU compositor uses `zpos` per plane.
   Default mapping: animated_layer_index → zpos value. Static-text
   gets zpos=1 (under all animated layers? on top of all? — needs
   pick; spec says "later array entries paint on top" so static
   should default to LOWEST zpos among text content, beat only by
   bg primary).
5. **Cost estimate** — multi-plane compositor is ~3-5 days of
   careful work: DRMRenderer extension, GPUSlideCompositor, each
   effect's property-mapping math, integration with PlaybackLoop's
   `_play_dynamic_slide`, end-to-end test on the dev Pi at 1080p.
   Plus the dev-config switch (sign 128×96 → 1920×1080 in
   phase6_welcome_loop.py and smoke scripts). Confirm cost before I
   start.

## Implementation order (if approved)

1. **DRMRenderer extension** — additive: `attach_animated_layer`,
   `update_animated_layer`, `detach_animated_layer`, `commit`.
   Existing `render_frame` path stays for the LED-matrix software
   fallback. Tests + Pi smoke.
2. **GPUSlideCompositor** — pure logic, takes a slide + renderer
   handle, drives the per-frame property updates. Effect-to-
   property math mirrors `motion.py`. Tests against a fake-
   renderer that records property updates.
3. **PlaybackLoop integration** — `_play_dynamic_slide` branches:
   if `renderer` has the multi-plane API + `slide_has_motion`,
   route through GPUSlideCompositor; else current
   compose_motion_frame path.
4. **Dev-config switch** — phase6_welcome_loop.py default sign
   dims 128×96 → 1920×1080. Smoke scripts likewise. (qarl 2026-05-
   02: "stop thinking about low-rez for a while.")
5. **Live-fire** — Welcome loop on dev Pi at 1080p with motion =
   ticker / breathe / pulse / bounce / shake / blink, eyeball
   confirms motion + plane stacking + alpha all hardware-driven.
6. **Bench** — re-run `phase6_motion_bench.py` against the new
   compositor at 1080p; expect <1 ms / frame across all effects.

## Step 1 implementation notes (landed 2026-05-02)

DRMRenderer extension + smoke shipped. The actual API drifted from
the design above on two points; both came out of the live-fire
smoke (`scripts/phase6_drm_compositor_smoke.py`):

- **Dropped the dedicated static-text plane.** Software-composite bg
  + every static text layer into the *primary* plane at slide entry
  (existing `render_frame` path). The orchestrator does this once
  per slide; only animated layers consume overlay planes. Reasons:
  (a) at 1080p uncropped ARGB8888, vc4 LBM caps simultaneous active
  planes at 3 — keeping a static-text plane permanently bound burned
  one of those slots for content that doesn't change. (b) The
  primary plane is XRGB8888 anyway and software-composite of N
  static layers is a one-time cost, not a per-frame cost.
- **Animated planes write the glyph bbox subregion, not the full
  sign rect.** `attach_animated_layer` takes `src_w/src_h` (the
  caller's bbox dims) + `crtc_x/y/w/h` (where the layer sits on
  display). The plane fb is still allocated at sign-native dims
  (so we don't reallocate per slide), but only the top-left
  bbox is written and `SRC_W = bbox_w << 16`. vc4 LBM scales with
  SRC_W not fb width, so cropped sources stay well under the LBM
  ceiling (smoke confirmed: 3 simultaneous planes at 878-wide and
  753-wide bboxes commit cleanly at 1080p).

### vc4 alpha-handling gotchas

Two non-obvious behaviors burned a couple of debugging cycles:

- **`pixel blend mode = COVERAGE` on vc4 ignores per-pixel alpha.**
  Documentation says COVERAGE multiplies the *effective* alpha by
  per-pixel-alpha × plane.alpha; vc4 in practice applies plane.alpha
  as a global coverage and treats every pixel as fully opaque. With
  glyph-bbox-cropped text this renders the bbox transparent area
  as opaque-black through plane.alpha. Don't use COVERAGE on vc4.
- **`pixel blend mode` persists across DRM master sessions.** If a
  prior process set COVERAGE on a plane id, the next process that
  attaches to that plane inherits COVERAGE — even though vc4's
  documented default is PREMULTI. `attach_animated_layer` now
  explicitly stages PREMULTI every attach so prior state cannot
  haunt us.
- **PREMULTI requires premultiplied input.** `_write_plane_buffer_
  subregion` runs `ImageChops.multiply(channel, alpha)` per RGB
  channel before swizzling to BGRA byte order. Under PREMULTI +
  premultiplied input + plane.alpha multiplier, transparent bbox
  pixels stay (0,0,0,0) at any plane.alpha and ink pixels fade
  cleanly toward 0 — pulse / blink work as designed.

### Behavioral divergences vs. the software path

Step 2's `GPUSlideCompositor` mirrors `motion.compose_motion_frame`'s
intent but produces visibly different pixels in two places:

- **Ticker — sweep instead of in-box wrap.** Software uses
  `np.roll` on the box-cropped region, so text leaving the left
  edge re-enters from the right (continuous marquee, no snap). The
  GPU path sweeps the cropped glyph horizontally across the box's
  on-screen extent and snaps back at phase wrap. A future iteration
  could pre-render text twice into a 2*box_w-wide source bitmap
  and slide SRC_X for a true wrap, but that needs a buffer-vs-src
  dim split in `attach_animated_layer`. Deferred until operators
  flag the snap as a problem.
- **Breathe — HVS bilinear instead of PIL NEAREST.** Software
  scales the glyph via `Image.Resampling.NEAREST` (motion.py:151).
  GPU delegates scale to vc4 HVS, which is bilinear. The GPU
  breathe will look smoother than the editor's preview at the same
  intensity. Operator-visible but not wrong.
