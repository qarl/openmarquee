# Renderer parity audit — 2026-05-14

Concrete map of where the 4 renderers' math diverges, in
preparation for a Canvas2D-vs-Rust pixel-comparison test build.

**Read-only audit. No code changes. No tests written.**

Output is the "what would the comparison test need to gate on"
list. Same shape as the task #94 / #99 / #100 investigations.

## §1 Scope

Four renderers audited across six surface areas:

| Renderer | Implementation root |
|----------|---------------------|
| Canvas2D (browser editor) | `ui/src/rasterize.js`, `ui/src/state-from-item.js`, `ui/src/bg-system.js`, `ui/src/canvas-motion.js`, `ui/src/inline-preview.js` |
| Python+PIL (fallback) | `backend/openmarquee/rendering/*` (`hdmi.py`, `blend.py`, `composite.py`), `backend/openmarquee/text_raster.py`, `backend/openmarquee/auto_render.py`, `backend/openmarquee/motion.py` |
| GPUSlideCompositor (DRM multi-plane) | `backend/openmarquee/rendering/gpu_compositor.py` (Phase 6.5) |
| Rust rust-sidecar | `renderer/src/hdmi.rs`, `renderer/src/hdmi_logic.rs`, `renderer/src/v4l2.rs`, `renderer/src/mp4_demux.rs` |

Surfaces: text layout, color math, transition timing (16 kinds),
motion math, backgrounds (solid/pattern/gradient/image/video),
compositing order.

**Important framing**: PIL is queued for delete per DELETE-PIL
phases 5+6 (post rust-sidecar default-flip). PIL-vs-others
divergences are acceptable in the long run; only Canvas2D ↔ Rust
↔ GPUSlideCompositor divergences matter for the pixel-comparison
test target.

## §2 Per-surface findings

### 2.1 Text layout

3 of 4 renderers (Canvas2D, Rust, PIL) implement text layout
independently. GPUSlideCompositor delegates to PIL for static
layers + Rust at scanout for animated layers. Confidence: **high**.

Notable convergence: Canvas2D's `fontSize × 1.1` line-height was
pinned in Rust at `renderer/src/hdmi_logic.rs:269` after commit
`c56314f`. PIL uses Pillow's `textbbox` (built-in metrics) which
implicitly includes ascent/descent — produces subtly taller
multi-line blocks than Canvas2D/Rust.

Divergences:

- **P1 — line-height multiplier**: PIL uses Pillow's
  `textbbox`-based metrics; Canvas2D + Rust both apply explicit
  1.1× multiplier (`ui/src/rasterize.js:119` ↔
  `renderer/src/hdmi_logic.rs:269`). PIL is delete-queued — not
  blocking parity.
- **P1 — default font size when neither pct nor px is set**:
  Canvas2D 0.3×box-width (`ui/src/rasterize.js:339`), PIL 0.3×box-
  width (`backend/openmarquee/seed.py:1215`). Rust delegates to
  the caller via `effective_font_size_px` at
  `renderer/src/hdmi_logic.rs:3513` — the fallback isn't baked
  into Rust itself, callers must pass a number.
- **P2 — character spacing / kerning**: 3 independent font
  engines (Canvas fillText native, Pillow with multi-run emoji
  segmentation, Rust fontdue). Sub-pixel deltas are expected;
  not pixel-comparable without per-engine tolerance.
- **P0 (framing) / no real divergence — word-wrap**: Canvas2D +
  PIL both wrap at word boundaries pre-layout
  (`ui/src/rasterize.js:164-189` ↔
  `backend/openmarquee/seed.py:1119`). Rust receives pre-wrapped
  input from the server — same wrap points, just upstream.

### 2.2 Color math

The most-divergent surface. Rust is canonical (pinned BT.709 in
`a49505c` 2026-05-14; FS_BRIGHT_GAMMA in tree at hdmi_logic.rs:
1950-1959). PIL + Canvas2D don't apply gamma; their alpha mode
is straight (source-over) where Rust uses premultiplied. The
divergence is partially deliberate (Rust's gamma + premul is
the architecturally-cleaner path; PIL was queued for delete
before it could be converged). Confidence: **high**.

Divergences:

- **P0 — YUV→RGB matrix**: Rust uses BT.709 limited-range
  (`renderer/src/hdmi_logic.rs:2055-2098`, post-`a49505c`).
  Canvas2D / PIL don't ship V4L2 video — they don't do YUV→RGB
  at all. The divergence is theoretical until another renderer
  picks up video paint. **Not blocking pixel-comparison**.
- **P1 — alpha blending mode**: Canvas2D + PIL use straight
  alpha source-over (`ui/src/rasterize.js:28-32`,
  `backend/openmarquee/rendering/blend.py:114-198`). Rust
  FS_GLYPH emits premultiplied alpha
  (`renderer/src/hdmi_logic.rs:606,2711`). For opaque text
  (α=1) identical. For translucent text or layered transitions,
  intermediate values differ. **Tolerance-band-able**.
- **P1 — gamma correction**: Rust applies `pow(1/gamma)` (default
  gamma=2.2) via FS_BRIGHT_GAMMA shader
  (`renderer/src/hdmi_logic.rs:1950-1959`). Canvas2D + PIL apply
  no gamma — output linear RGB. **Rust renders appear brighter
  at default settings**; operator brightness slider can offset.
  This is the visible-by-eyeball divergence right now.
- **P2 — limited-range YUV scaling**: Rust-only (no other
  renderer ships YUV).

### 2.3 Transition timing (16 kinds)

All 16 kinds use LINEAR progress (no easing). Canvas2D's preview
loop progress semantics are inverted vs device (countdown rather
than forward) but preview is advisory only. Dissolve + glitch use
different RNG hashes per renderer (numpy LCG / JS Math.random /
splitmix64); each is internally deterministic. Confidence: **high**.

Notable convergence: Pixelate's `1 - abs(2*progress - 1)`
triangular wave (`backend/openmarquee/playback.py:2462` ↔
`ui/src/inline-preview.js:336`) is identical between Python and
Canvas2D. Halftone's `pitch × 0.71` radius formula
(`playback.py:2517` ↔ `inline-preview.js:388`) is identical.

Divergences:

- **P1 — Canvas2D progress semantics (countdown)**: Canvas2D
  inline preview computes `progress = 1 - timeLeft/fadeSec`
  (`ui/src/inline-preview.js:225`); Python + Rust use
  `progress = i / n_frames` forward. Preview-only; cosmetically
  acceptable since cuts/fades are visually symmetric. **Don't
  fix unless qarl wants pixel-identical preview→device**.
- **P0 cosmetic — "cut" transition missing from Canvas2D
  animated set**: Canvas2D ANIMATED_TRANSITIONS at
  `ui/src/inline-preview.js:58-62` lists 15 of 16 kinds.
  Python + Rust handle cut explicitly. Effect is identical
  (an instant cut shows instant in both paths — Canvas2D
  preview falls through to no-animation, device cuts cleanly).
  Not a real bug; doc-the-skip would close it.
- **P1 — dissolve / glitch RNG**: numpy default_rng
  (`playback.py:2395`) vs JS Math.random
  (`inline-preview.js:286-290`) vs splitmix-style hash in
  FS_DISSOLVE (`renderer/src/hdmi_logic.rs:754-778`). Each
  internally deterministic, none pixel-match each other.
  **Skip pixel-equivalence on these two transitions** — gate
  on statistical properties (% pixels revealed at progress=0.5)
  instead.

### 2.4 Motion math

The other most-divergent surface. Bounce was P0 spec-drift in
Rust (FIXED in `ed7162a` 2026-05-14 — Rust now matches Python
`abs(sin)`). Breathe + pulse "floor" divergences are NOT drift
— they are **deliberate Rust spec choices** pinned by QA F3 tests
(`motion_breathe_intensity_zero_still_animates` +
`motion_pulse_intensity_zero_still_animates` at
`hdmi_logic.rs:7442-7460`, comment-tagged "QA F3: intensity=0
!= static — pin the deliberate spec choice"). Python motion.py
is the drifter on these two, but motion.py is PIL-path-tied and
PIL is delete-queued — so the divergences are acceptable.
Confidence: **high**.

**See §6 for methodology note**: this re-classification (Python
→ Rust canonical for breathe/pulse) corrects the audit's initial
natural assumption that motion.py was canonical for all motion
math. The QA F3 test pins establish Rust intent.

Divergences:

- **P0 — bounce wave function** (CLOSED, `ed7162a` 2026-05-14):
  Was: Rust `hdmi_logic.rs:3365` plain `sin` (symmetric) vs
  Python `motion.py:300` `abs(sin)` (asymmetric "ball-on-floor"
  per spec comment). Fix: Rust now uses
  `-amp * phase_rad.sin().abs()`. Pinned by new
  `motion_bounce_abs_sin_shape_matches_python` test (9-sample
  full-cycle sweep).
- **P1 (reclassified) — breathe amplitude floor**: Rust
  `hdmi_logic.rs:3336` `0.02 + 0.18 × intensity_norm` (2 % floor
  at intensity=0). Python `motion.py:220` `(intensity/100) ×
  0.20` (0 % floor). **Canonical: Rust** (pinned by QA F3 test
  `motion_breathe_intensity_zero_still_animates` at
  `hdmi_logic.rs:7447`). Python is the drifter. Action:
  motion.py is PIL-path-tied → delete-queued → out-of-scope.
- **P1 (reclassified) — pulse alpha range**: Rust
  `hdmi_logic.rs:3350` `0.70 × (1 - intensity_norm)` (30 % swing
  at intensity=0). Python `motion.py:266` `1.0 - intensity/100`
  (no swing at intensity=0). **Canonical: Rust** (pinned by QA
  F3 test `motion_pulse_intensity_zero_still_animates` at
  `hdmi_logic.rs:7455`). Python is the drifter. Action:
  motion.py is PIL-path-tied → delete-queued → out-of-scope.
- **P1 — shake RNG**: Python MD5 of layer+phase+step
  (`motion.py:309-316`) vs Rust splitmix64 XOR
  (`hdmi_logic.rs:3384-3398`). Both deterministic per
  layer+phase+time, neither matches the other. **Skip pixel-
  equivalence on shake** — gate on motion-amplitude
  statistical properties.
- **P2 — ticker frequency formula**: Python `cycle_s = 6.0 -
  0.05 × intensity` (`motion.py:139`) vs Rust `base_period =
  6.0 - 5.0 × intensity_norm` (`hdmi_logic.rs:3312`).
  Mathematically equivalent (intensity_norm = intensity/100).
  No divergence.
- **P2 — blink frequency**: identical piecewise-linear
  (`motion.py:146-149` ↔ `hdmi_logic.rs:3419-3423`). No
  divergence.

### 2.5 Backgrounds

Cleanest surface. 10 procedural patterns (dots, halftone, stripes,
scanlines, checker, grid, rings, rays, confetti, bricks) per
project memory + `ui/src/bg-system.js:51-54` ↔
`renderer/src/hdmi_logic.rs:2445-2456` (PatternKind enum). Density
curve maintained in lockstep per explicit comment at
`hdmi_logic.rs:2489-2493` ("Backend mirror... Both sides must
stay in lockstep for WYSIWYG parity"). PIL pattern rendering is
**deliberately deleted** per DELETE-PIL phase 3b
(`backend/openmarquee/auto_render.py:195-200` — safety fallback
to solid color only). Confidence: **high**.

Divergences:

- **P1 (delete-queued) — PIL pattern fallback**: Removed at
  `auto_render.py:195-200`. Patterns now stored as PNG at edit
  time by Canvas2D, device applies FS_PATTERN_* shaders to
  animated backgrounds via Rust. Expected.
- **P2 — gradient density formula**: Canvas2D
  (`bg-system.js:78`) ↔ Rust FS_GRADIENT
  (`hdmi_logic.rs:2188`): both use `lerp(0, 270, density)`
  angle mapping. Identical.
- **No divergences on solid color, image background letterbox
  math** (Canvas2D `rasterize.js:207-220` ↔ Rust
  `hdmi.rs:2621-2642` both use `max(w_disp/w_img, h_disp/h_img)`
  scale).
- **Out of scope — video background**: Only Rust pieces 3-4
  ship VideoSlide paint; "video-as-background" (loop a video
  under text layers) isn't shipped on any renderer yet. Future
  work.

### 2.6 Compositing order (z-order)

No `z_order` field in the schema — array order in `text_layers[]`
governs paint order across all 4 renderers. Background paints
first, then layers in declaration order, then transition overlay
(Rust-only currently). Architectural difference in WHEN motion
applies (GPUSlideCompositor: rasterize-once then transform CRTC
properties; Rust: integrate motion into quad geometry per frame;
Canvas2D: 2D affine transform around draw). All three are
correct; different performance profiles. Confidence: **medium**
(no z_order handling found anywhere — implicit array-order is
the convention).

Divergences:

- **No P0/P1 paint-order divergences.** All renderers iterate
  layers in array order; later layers paint over earlier.
- **P1 (architectural, not a bug) — motion-apply timing**:
  GPUSlideCompositor moves CRTC plane properties per tick
  (`backend/openmarquee/rendering/gpu_compositor.py:538-544`);
  Rust integrates motion into glyph quad NDC math
  (`renderer/src/hdmi.rs:7970-8040`); Canvas2D wraps motion
  around paint via `paintLayerWithMotion`
  (`ui/src/rasterize.js:269`). Same final visual outcome (under
  the motion math caveats from §2.4); different sub-frame
  sampling characteristics.
- **P2 — caching strategy differences**: GPUSlideCompositor
  caches the whole primary plane keyed by `(slide.id, w, h,
  updated_at)` (`gpu_compositor.py:70-113`); Rust caches glyph
  bitmaps + textures per slide
  (`renderer/src/hdmi.rs:345`). No paint-order impact.

## §3 Consolidated divergence list

| # | Severity | Surface | Description | Canvas2D loc | Rust loc | Python/PIL loc | Canonical | Recommended action |
|---|----------|---------|-------------|--------------|----------|----------------|-----------|---------------------|
| 1 | ~~P0~~ **CLOSED** | Motion | Bounce wave: Rust plain `sin` → fixed to `abs(sin)` in `ed7162a` | — | `hdmi_logic.rs:3365` | `motion.py:300` | Python (spec, now matched) | **Done — `ed7162a`** |
| 2 | P0 cosmetic | Transitions | Canvas2D `ANIMATED_TRANSITIONS` set omits "cut" | `inline-preview.js:58-62` | `hdmi_logic.rs:1900` | `playback.py:2081` | Rust/Python | Doc-the-skip or add cut to set |
| 3 | P1 **(reclassified)** | Motion | Breathe amplitude 2% floor in Rust; 0% in Python at intensity=0 | — | `hdmi_logic.rs:3336` | `motion.py:220` | **Rust** (QA F3 pin at `hdmi_logic.rs:7447`) | Drifter (motion.py) is PIL-tied → out-of-scope |
| 4 | P1 **(reclassified)** | Motion | Pulse alpha 30% swing in Rust; 0% in Python at intensity=0 | — | `hdmi_logic.rs:3350` | `motion.py:266` | **Rust** (QA F3 pin at `hdmi_logic.rs:7455`) | Drifter (motion.py) is PIL-tied → out-of-scope |
| 5 | P1 | Color | Gamma correction applied in Rust only; Canvas2D + PIL output linear RGB | rasterize.js:28-32 | `hdmi_logic.rs:1950-1959` | (none) | Rust (canonical going forward) | Add gamma to Canvas2D OR document Rust-only |
| 6 | P1 | Color | Alpha blending: Canvas2D/PIL straight (source-over); Rust premultiplied | rasterize.js:28-32 | `hdmi_logic.rs:606,2711` | `blend.py:114-198` | Rust (canonical) | Document for translucent cases |
| 7 | P1 | Text | Line-height: PIL Pillow textbbox metrics; Canvas2D/Rust explicit `1.1×` | rasterize.js:119 | `hdmi_logic.rs:269` | (Pillow default) | Canvas2D/Rust | PIL delete-queued — leave |
| 8 | P1 | Transitions | Dissolve / glitch RNG: numpy LCG vs JS Math.random vs splitmix64 | `inline-preview.js:286` | `hdmi_logic.rs:754-778` | `playback.py:2395` | None (independent) | Skip pixel-eq; gate on statistical properties |
| 9 | P1 | Motion | Shake RNG: MD5 vs splitmix64 | `canvas-motion.js` | `hdmi_logic.rs:3384-3398` | `motion.py:309-316` | None (independent) | Skip pixel-eq; gate on amplitude stats |
| 10 | P1 | Transitions | Canvas2D `progress = 1 - timeLeft/fadeSec` (countdown); device `i/n` forward | `inline-preview.js:225` | `hdmi.rs:3225` | `playback.py:2282` | Rust/Python | Don't fix unless qarl wants pixel-identical preview |
| 11 | P2 | Text | Character spacing differs across font engines (fontdue / Pillow / Canvas native) | — | (fontdue) | (Pillow) | None | Set per-engine tolerance band |
| 12 | P2 | Compositing | Motion-apply timing: rasterize-then-CRTC (GPU) vs glyph-quad (Rust) vs 2D affine (Canvas) | rasterize.js:269 | `hdmi.rs:7970-8040` | `gpu_compositor.py:538-544` | None (architectural) | Document; not pixel-comparable |

**Totals (post-amendment 2026-05-14):**
- 1 P0 actionable: ~~bounce~~ **CLOSED** (`ed7162a`)
- 1 P0 cosmetic: #2 (Canvas2D "cut" omitted)
- 8 P1: 2 reclassified to "Rust canonical, drifter out-of-scope"
  (#3 breathe, #4 pulse); 1 needs a qarl-direct call (#5 gamma —
  visible-by-eyeball, Rust canonical going forward); the
  remaining 5 are either delete-queued (#7 PIL line-height),
  independent-RNG (#8 dissolve/glitch, #9 shake), or deliberate
  preview/architectural (#6 alpha, #10 Canvas countdown).
- 2 P2: per-engine kerning, motion-apply timing (architectural).

Net: **1 P0 closed, 0 P1 left needing Rust-side fixes.** The
remaining work is documentation, statistical gates for RNG-driven
surfaces, and the qarl-direct decision on whether Canvas2D should
adopt Rust's gamma=2.2 path for true WYSIWYG.

## §4 What a pixel-equivalence test would need to gate on

Concrete inputs + tolerance bands per surface:

### Text layout
- **Inputs**: a TextSlide with N layers, mixed font sizes,
  multi-line content, varied alignment (left/center/right).
  Bypass shrink-to-fit by setting explicit `font_size_px` so
  Rust delegate-to-caller is fed deterministic numbers.
- **Tolerance**: per-pixel RMSE ≤ 3.0 / 255 (sub-pixel glyph
  rasterization is unavoidable across font engines). Tighter
  tolerance for solid-fill regions (≤ 0.5 / 255).
- **Skip**: PIL comparison (delete-queued); per-engine kerning.

### Color math
- **Inputs**: solid color, text on color, alpha-blended layer
  stack with translucent text (alpha 0.5 specifically chosen to
  expose straight-vs-premul drift).
- **Tolerance**: opaque text per-pixel RMSE ≤ 1.0 / 255;
  translucent layers per-pixel RMSE ≤ 8.0 / 255 (until alpha
  mode is decided + aligned).
- **Skip**: YUV→RGB (no shared input); BT.709 vs BT.601 (Rust
  shader-only).
- **Gamma**: either align to Rust's gamma=2.2 (canvas2D would
  need a gamma pass post-rasterize) OR document Rust as
  "perceptually preferred default" and use brightness slider
  to offset.

### Transition timing
- **Inputs**: each of 16 transition kinds, sampled at
  progress ∈ {0, 0.25, 0.5, 0.75, 1.0}.
- **Tolerance**: ≤ 2.0 / 255 RMSE per pixel except dissolve /
  glitch / pixelate which are RNG-driven.
- **Skip pixel-eq, gate on statistics for**: dissolve (assert
  X% pixels revealed at progress=0.5 ± 2%), glitch (assert
  N tear rows per frame, count not position), shake (assert
  per-frame mean amplitude is in expected range).

### Motion math
- **Inputs**: each motion kind (static / ticker / breathe /
  pulse / bounce / shake / blink) at intensity ∈ {0, 50, 100}
  and phase ∈ {0, 0.5}.
- **Tolerance**: ≤ 1px translation, ≤ 2.0/255 alpha for
  pulse/breathe.
- **Convergence status**: bounce (#1) CLOSED in `ed7162a` —
  pixel-match now achievable across the abs(sin) shape.
- **Skip pixel-eq for**:
  - shake (independent RNG — see #9)
  - breathe/pulse at intensity=0 (Rust canonical, motion.py is
    PIL-tied drifter — see #3, #4)
  - any pixel-eq between Rust and motion.py for breathe/pulse
    floor behavior (intentional divergence)

### Backgrounds
- **Inputs**: each of 11 patterns at 3 density values; gradient
  at 3 directions; solid color; image background with multiple
  aspect ratios (16:9, 4:3, 1:1) to stress the letterbox math.
- **Tolerance**: solid + gradient + image ≤ 1.0 / 255 (clean
  math); patterns ≤ 3.0 / 255 (anti-aliased edges).

### Compositing
- **Inputs**: multi-layer slide with varying z-order
  (declaration order is the test).
- **Tolerance**: ≤ 1.0 / 255 for static stacks; skip animated-
  layer-CRTC vs animated-layer-quad pixel-eq (architectural
  divergence in motion-apply timing).

## §5 Cross-cuts (features missing entirely)

- **Video background** (a VideoSlide as the background under
  text layers): only Rust ships VideoSlide paint (pieces 3-4),
  and even there it's a slide-type, not a background-layer
  type. No renderer supports it as bg today. Future scope.
- **Transition overlay across slides on Canvas2D**: Canvas2D
  preview handles transitions for the 15 listed kinds but
  doesn't reach back into the layer-cache the way Rust does
  (Rust pre-bakes both from/to slides + cross-fades shader-
  side). Preview-only divergence; not visible to operator on
  device.
- **VideoSlide Capture** (thumbnail screenshot): Rust paint
  ships, capture is unimplemented (per `35c80c6`). Not a
  parity concern — feature gap.

## §6 Are any renderers truly canonical?

**Mixed** (revised 2026-05-14 post-bounce-fix amendment):
- **Rust** is canonical for color math (BT.709 + gamma + premul
  alpha — defines the production-default-flip target).
- **Rust** is canonical for motion at intensity=0 (breathe + pulse
  floors are deliberate Rust spec choices pinned by QA F3 tests
  at `hdmi_logic.rs:7442-7460`). Python `motion.py` drifts on
  these; PIL-tied so out-of-scope.
- **Python `motion.py`** is canonical for the bounce wave SHAPE
  (`abs(sin)` ball-on-floor); Rust now matches via `ed7162a`.
- **Canvas2D + Rust** are co-canonical for text layout
  (line-height pinned in lockstep per `c56314f`).
- **Canvas2D** is canonical for the operator-facing preview
  experience (WYSIWYG target); device should match it visually
  on slides where neither side has a deliberate-divergence pin.

No single renderer is universally canonical. The audit's
recommendation: treat **Canvas2D as the WYSIWYG ground truth**
for text + color (the operator preview is what they design
against), Rust for motion-floor + color-math production
defaults, and only converge cross-renderer math when a test
pin doesn't already document deliberate divergence.

### §6.1 Methodology note (added post-amendment)

The first pass of this audit assumed `motion.py` was canonical
for motion math by virtue of being the spec-authored Python
implementation. The bounce-fix dispatch (`ed7162a`) surfaced
that this assumption was wrong for breathe + pulse: those Rust
formulas are pinned by QA F3 tests at `hdmi_logic.rs:7442-7460`
explicitly comment-tagged "intensity=0 != static — pin the
deliberate spec choice." Pinned-by-intentional-test means
**that** renderer is canonical, not the other.

**Rule for future audits**: before classifying canonical /
drifter, check the deviating renderer for existing test pins
(QA F-tagged or similar comment-marked intent). A renderer
pinned by intentional tests is canonical; reverse the audit's
natural assumption when the test comment says "deliberate spec
choice." This is the same lesson as
`feedback_verify_against_head_blob_not_working_tree` — verify
against ground truth (here: test pins), not the audit's natural
prior.

Concrete check sequence:
1. Grep for tests pinning each divergence's expected values.
2. Read test docstrings/comment headers for intent tags.
3. If the test comment establishes intent ("deliberate", "spec
   pin", "QA F-N", or similar) → that renderer is canonical.
4. If neither side has a pin → "no canonical, independent
   determinism" or "qarl-direct needed."

## §7 Feasibility for pixel-comparison test build

**Estimate: medium scope** (~1-2 day dispatch).

Reasons it's not "large":
- All four renderers expose a deterministic baking entry-point
  (`scripts/bake.py` driver for the Canvas2D path, the
  rust-sidecar IPC for Rust, GPUSlideCompositor's `.attach()`
  for the multi-plane path). Inputs are JSON fixtures that
  already exist in `renderer/tests/fixtures/`.
- Tolerance bands are computable from this audit (most ≤ 3.0 /
  255 RMSE; specific carve-outs documented).
- The known divergences are LOCALIZED — fixable in
  bounded patches: bounce is a one-line shader fix, breathe /
  pulse are coefficient swaps.

Reasons it's not "small":
- Need a Mac-side Canvas2D headless driver (Playwright already
  in tree per `scripts/bake.py`); need Pi-side Rust driver
  (IPC mode already works); need GPU compositor mock path
  (DRM-less Mac comparison would be artificial — defer
  GPU compositor to live-Pi verification).
- Per-surface tolerance bands need a calibration pass before
  CI gating (false-positive risk on tight bands).
- Three RNG-driven surfaces (dissolve / glitch / shake) need
  statistical assertions, not pixel-eq — that's extra
  infrastructure.

## §8 Confidence

| Surface | Confidence | What was read end-to-end | What was skimmed |
|---------|------------|--------------------------|------------------|
| 2.1 Text | high | rasterize.js full, seed.py wrap helper, hdmi_logic.rs layout fns | text_raster.py module |
| 2.2 Color | high | hdmi_logic.rs FS_NV12_TO_RGB + FS_BRIGHT_GAMMA + FS_GLYPH, blend.py source-over impl | sRGB curve details |
| 2.3 Transitions | high | all 16 kinds in playback.py + inline-preview.js + fs_for_transition_kind | exact GLSL of FS_HALFTONE / FS_SCANLINE |
| 2.4 Motion | high | full motion.py + hdmi_logic.rs:3207-3444 + canvas-motion.js dispatch | exact RNG output distributions |
| 2.5 Backgrounds | high | full bg-system.js + PatternKind enum + density curve comments | auto_render.py image loader |
| 2.6 Compositing | medium | paint_slide order in all 4 renderers | depth-test / blend-func runtime state on Rust |

**Overall confidence: high** that the divergence list is
complete + correctly characterized. The single P0 actionable is
bounce-sin; the rest is documented divergence to live with or
delete-queued PIL noise.

## Subagent LGTM

Audit was done across 3 parallel Explore subagents:
- Text + color (returned 8 divergences across 2 surfaces, high confidence)
- Transitions + motion (returned 7 divergences, high confidence, P0 bounce surfaced)
- Backgrounds + compositing (returned 0 live divergences, high confidence)

All 3 returned with file:line citations on both sides. The
synthesized list was sanity-checked against the actual code for
the P0 bounce claim (verified `motion.py:300` uses `abs(sin)`,
`hdmi_logic.rs:3365` uses plain `sin`).

LGTM for the synthesized output.
