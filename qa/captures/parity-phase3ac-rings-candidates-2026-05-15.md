# Phase 3ac: RINGS probe — diagnosis was wrong, surfaced as design question

**Date:** 2026-05-15
**Dispatch hypothesis:** vc4 mediump fragment-shader half-float overflow on
`length()` at corner pixels (squared magnitudes ~1.2M > mediump max ~6.5e4).
**Captures:** /tmp/phase3ac-cand-G.png (Pi capture with Cand G applied),
/tmp/phase3ac-baseline-rust.png (current Pi golden), /tmp/phase3ac-canvas2d-ref.png
(Canvas2D-via-Playwright reference).

## Cand G implemented + tested → made NO visible change on Pi

Implementation: precompute `u_radial_scale = 1 / sqrt((W/2)^2 + (H/2)^2)`
and `u_max_radius = sqrt((W/2)^2 + (H/2)^2)` on the CPU. Shader becomes:

```glsl
vec2 pos = vec2(gl_FragCoord.x, u_viewport.y - gl_FragCoord.y);
vec2 d = pos - u_viewport * 0.5;
float dist_normalized = length(d * u_radial_scale);  // d² in [0, 1]
float dist = dist_normalized * u_max_radius;
float period = mod(dist, u_tile);
float t = step(u_threshold, period);
```

Pi-on-glass result with Cand G:
- render_tests.sh: PASS, 62 pixels differ at max_delta=229 (= Cause B text
  glyph floor; rings math output bit-identical to pre-fix Pi capture).
- parity_tests.sh: unchanged (compares against pre-fix golden which is the
  same as Cand G output).

Conclusion: the mediump-overflow hypothesis was **wrong**. vc4 evidently
handles `length()` via higher-precision intermediates even when declared
mediump, so the corner-pixel dot product doesn't actually overflow in
practice. Cand G was a no-op.

Cand H + Cand K not run — the root cause is not precision. (Cand H was
y-flip cleanup, would also be a no-op for radial distance. Cand K
vertex-passed varying would also be unnecessary.)

## Actual divergence: SEMANTIC, not precision

Visual inspection of golden vs Canvas2D reference shows TWO DIFFERENT
RENDERINGS of "rings":

- **Pi renderer (Rust GLES2 shader)**: alternating wide concentric bands.
  Each period of `tile` pixels is split into `half-2` pixels of color_a
  and `half+2` pixels of color_b. At default tile=92 (raw density=0.5),
  that's ~44 pixels orange + ~48 pixels blue alternating outward. Looks
  like a bullseye target.

- **Canvas2D renderer (ui/src/bg-system.js paintPatternOnCanvas)**: thin
  2-pixel-wide concentric ring strokes at radii `half`, `half+tile`,
  `half+2*tile`, ... over a solid color_a background. Looks like contour
  lines / sonar pings.

Both have used these renderings since their respective fixtures landed.
Neither has "drifted" — they were implemented to different specifications.

## Three sources of truth, all disagree

1. **Rust shader docstring** (`hdmi_logic.rs:2392-2395`): "Concentric rings
   around the slide center. Period-`u_tile` repetition: each period has
   a color_a band of (half-2) pixels followed by a 2-pixel color_b ring."
   → Describes thin rings (matches Canvas2D).

2. **Rust shader implementation** (`hdmi_logic.rs:2403-2409`):
   `step(u_threshold=half-2, mod(dist, tile))`. Result: alternating bands
   of `half-2` color_a and `half+2` color_b.
   → Wide alternating bands (does NOT match its own docstring).

3. **Canvas2D implementation** (`bg-system.js:367-380`): solid color_a
   background, then `ctx.stroke()` at lineWidth=2 for circles at radii
   `half + k*tile`.
   → Thin rings on solid background (matches Rust DOCSTRING but not its IMPL).

4. **Python backend** (`backend/openmarquee/auto_render.py`): grep'd, no
   `_render_pattern_rings` exists. Canvas2D + Rust are the only two
   live renderers; one of them is the "reference" by convention.

## Decision needed — direction not auto-decidable

Two design directions, both viable:

**Direction A: Make Rust match Canvas2D + Rust-docstring** — render thin
2-px rings on solid color_a. Shader becomes something like:
```glsl
float p = mod(dist, u_tile) - u_half;
float t = step(abs(p), 1.0);   // 1 when within +/-1 of ring center
```
Risk: changes the visual appearance of every existing slide that uses
the "rings" pattern. Slide editor previews would unchange (Canvas2D
already does this), but Pi-rendered HDMI output would change visibly.

**Direction B: Make Canvas2D match Rust impl** — `paintPatternOnCanvas`
"rings" branch should fill alternating concentric bands instead of
stroking thin rings. Slide editor previews would change to match
HDMI Pi output. Authored slides with rings would look bolder.

**Direction C: Treat as bug in Rust impl; honor docstring** — drop the
`u_threshold` uniform, replace with `u_half` and the abs-difference
test from Direction A. Same effect on Pi rendering as Direction A.

## What this means for the playbook

The Phase 3 arc has been **structural-divergence ⇒ vc4-precision-fix**
end-to-end. Phase 3ac breaks that pattern: same SSIM-failure profile
(0.7146), but the divergence is in source semantics, not GPU precision.
The dispatch's hypothesis (length() overflow) was a reasonable guess
from the SSIM number alone, but direct visual inspection shows the
math IS correct on both sides — just different math.

The 4th playbook profile candidate ("length() precision variant of
Cand G/H/K") is NOT needed. There may still be vc4 mediump-`length()`
concerns at smaller viewport sizes or with future shaders, but rings
at 1080p doesn't manifest one.

## RAYS forecast

RAYS uses `atan(d.y, d.x)` + slice binning. Comparing
`ui/src/bg-system.js:383-399` (Canvas2D fills wedges via `ctx.arc`
sectors) to `hdmi_logic.rs:2417-2433` (shader atan2 + slice index +
parity), they appear to compute the same thing in compatible ways.
RAYS may still have actual mediump-precision issues (atan2 at large
operands), but rings doesn't inform that one way or the other.

## Verdict (pre-decision)

Rings divergence was design-level, not precision-level. QA was asked
to pick from directions A / B / C; greenlit Option C (honor the
docstring, match Canvas2D). Cand G code was reverted before the
semantic fix was applied.

## Shipped: Option C

Shader rewrite landed: thin-rings semantics matching Canvas2D's
`bg-system.js:367-380` and the existing Rust docstring at
`hdmi_logic.rs:2392-2395` (which was already correct -- the
implementation had silently drifted).

- `u_threshold` uniform replaced with `u_half`.
- Shader body: `step(abs(mod(dist, u_tile) - u_half), 1.0)` -> 3-pixel-wide
  color_b ring centered on each radius `k*u_tile + u_half`, approximating
  Canvas2D's `lineWidth=2` anti-aliased stroke coverage.
- `RingsUniforms::threshold` field renamed to `half`; `half = tile * 0.5`
  (no more `.floor()`; no more `max(0, half-2)` clamp).
- Renamed test `rings_uniforms_match_python_anchors` ->
  `rings_uniforms_match_canvas2d_anchors` to reflect reality
  (no Python `_render_pattern_rings` exists in `backend/openmarquee/`).

Parity metrics (parity_tests.sh) post-fix + re-bless:
- mean_delta: 59.108 -> **1.488** (-97%)
- SSIM:       0.7146 -> **0.9744** (+0.260)
- pct>=10:    49.08% -> **2.33%** (-95%)
- max=229 (Cause B text-glyph floor; same as all 7 prior shipped
  pattern fixtures)

Visible impact: existing slides using "rings" pattern will render
visibly differently on HDMI output (bullseye -> sonar ping). Editor
preview unchanged (Canvas2D already drew thin rings). Net: editor-HDMI
visual parity restored.
